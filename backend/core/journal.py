"""
Dziennik zmian — kopia przed kazda zatwierdzona zmiana i `/cofnij`.

Zanim Pipe wykona operacje zatwierdzona przez uzytkownika, zapisuje w
DATA_DIR/journal/<id>/ to, czego potrzeba do jej cofniecia:

  - kopie plikow, ktore operacja zmieni (tresc, uprawnienia, wlasciciel;
    plik, ktorego nie bylo, zostanie usuniety przy cofaniu),
  - komendy odwrotne dla efektow spoza plikow (docker stop -> docker start,
    systemctl disable -> enable, git pull -> git reset --keep <poprzedni HEAD>),
  - crontab uzytkownika, gdy operacja go zmienia.

Cofniecie tez jest zmiana: przed przywroceniem Pipe robi kopie stanu biezacego
(nowy wpis), wiec cofniecie mozna cofnac. Komendy odwrotne sa pokazywane przed
TAK i przechodza przez klasyfikator — zakazana komenda nie zostanie wykonana.

Kopie moga zawierac sekrety (np. edytowany .env) — katalog ma prawa 0700,
a pliki 0600. Trzymane jest MAX_ENTRIES ostatnich wpisow, nie dluzej niz KEEP_DAYS.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.core import memory

MAX_ENTRIES = 50
KEEP_DAYS = 14
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ENTRY_BYTES = 20 * 1024 * 1024


@dataclass
class FileBackup:
    local: str                 # sciezka widziana przez proces Pipe
    label: str                 # sciezka hosta albo opis ("pamiec Pipe: targets.json")
    existed: bool
    blob: str = ""             # nazwa pliku kopii w katalogu wpisu
    mode: int | None = None
    uid: int | None = None
    gid: int | None = None
    size: int = 0
    skipped: str = ""          # powod braku kopii (katalog, za duzy plik)


@dataclass
class Entry:
    id: str
    at: float
    interface: str
    tool: str
    command: str
    files: list[FileBackup] = field(default_factory=list)
    inverse: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    crontab: str | None = None
    status: str = "pending"    # pending | done | failed | aborted | restored | rolled_back
    exit_code: int | None = None
    verify: str = ""
    undo_of: str = ""

    @property
    def when(self) -> str:
        return datetime.fromtimestamp(self.at).strftime("%Y-%m-%d %H:%M")

    @property
    def undoable(self) -> bool:
        backed = any(f.blob or not f.existed for f in self.files if not f.skipped)
        # aborted — nic nie zmieniono; restored/rolled_back — zmiana juz odwrocona
        return self.status not in ("aborted", "restored", "rolled_back") and (
            backed or bool(self.inverse) or self.crontab is not None)

    def summary(self) -> str:
        state = {"pending": "w toku", "done": "wykonano", "failed": "blad", "aborted": "przerwano przed wykonaniem",
                 "restored": "cofnieto automatycznie", "rolled_back": "cofnieto"}.get(self.status, self.status)
        what = self.command if len(self.command) <= 120 else self.command[:117] + "..."
        extra = []
        if self.files:
            extra.append(f"kopie: {len([f for f in self.files if not f.skipped])}")
        if self.inverse:
            extra.append("komendy odwrotne")
        return f"#{self.id} {self.when} [{state}] {self.tool}: {what}" + (f" ({', '.join(extra)})" if extra else "")


def journal_dir() -> Path:
    return memory.data_dir() / "journal"


def _entry_dir(entry_id: str) -> Path:
    if not entry_id or not all(c in "0123456789abcdef" for c in entry_id):
        raise ValueError(f"Nieprawidlowy identyfikator wpisu: {entry_id!r}")
    return journal_dir() / entry_id


def _save(entry: Entry) -> None:
    directory = _entry_dir(entry.id)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    memory._write_atomic(directory / "entry.json", json.dumps(asdict(entry), ensure_ascii=False, indent=1) + "\n")


def load(entry_id: str) -> Entry | None:
    """Wpis albo None, gdy go nie ma. Nieprawidlowy identyfikator -> ValueError."""
    path = _entry_dir(entry_id) / "entry.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["files"] = [FileBackup(**f) for f in raw.get("files", [])]
        return Entry(**raw)
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _load_quiet(entry_id: str) -> Entry | None:
    try:
        return load(entry_id)
    except ValueError:
        return None


def entries(limit: int = MAX_ENTRIES) -> list[Entry]:
    """Wpisy od najnowszego."""
    directory = journal_dir()
    if not directory.is_dir():
        return []
    found = [_load_quiet(p.name) for p in directory.iterdir() if p.is_dir()]
    return sorted((e for e in found if e), key=lambda e: e.at, reverse=True)[:limit]


def latest_undoable() -> Entry | None:
    return next((e for e in entries() if e.undoable and e.status != "pending"), None)


def prune(now: float | None = None) -> None:
    now = now if now is not None else time.time()
    for index, entry in enumerate(entries(limit=10_000)):
        if index >= MAX_ENTRIES or now - entry.at > KEEP_DAYS * 86400:
            shutil.rmtree(_entry_dir(entry.id), ignore_errors=True)


# ─── Kopie ──────────────────────────────────────────────────────────────────

def _backup_file(directory: Path, index: int, local: str, label: str, budget: list[int]) -> FileBackup:
    if not os.path.lexists(local):
        return FileBackup(local, label, existed=False)
    if os.path.isdir(local) and not os.path.islink(local):
        return FileBackup(local, label, existed=True, skipped="katalog — bez kopii")
    try:
        stat = os.stat(local)
    except OSError as exc:
        return FileBackup(local, label, existed=True, skipped=f"brak dostepu ({exc.strerror})")
    backup = FileBackup(local, label, existed=True, mode=stat.st_mode & 0o7777, uid=stat.st_uid, gid=stat.st_gid,
                        size=stat.st_size)
    if stat.st_size > MAX_FILE_BYTES or stat.st_size > budget[0]:
        backup.skipped = f"plik za duzy na kopie ({stat.st_size} B)"
        return backup
    blob = f"{index}.bak"
    try:
        shutil.copyfile(local, directory / blob)
        os.chmod(directory / blob, 0o600)
    except OSError as exc:
        backup.skipped = f"nie udalo sie skopiowac ({exc.strerror or exc})"
        return backup
    budget[0] -= stat.st_size
    backup.blob = blob
    return backup


async def begin(interface: str, tool: str, command: str, *, files: list[tuple[str, str]] | None = None,
                inverse: list[str] | None = None, notes: list[str] | None = None,
                git_repos: list[str] | None = None, crontab: bool = False, undo_of: str = "") -> Entry:
    """Tworzy wpis i robi kopie PRZED wykonaniem operacji. `files` — pary (sciezka lokalna, etykieta)."""
    from backend.core import executor

    entry = Entry(secrets.token_hex(4), time.time(), interface, tool, command,
                  inverse=list(inverse or []), notes=list(notes or []), undo_of=undo_of)
    directory = _entry_dir(entry.id)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    budget = [MAX_ENTRY_BYTES]
    seen: set[str] = set()
    for index, (local, label) in enumerate(files or []):
        if local in seen:
            continue
        seen.add(local)
        entry.files.append(_backup_file(directory, index, local, label, budget))
    for repo in git_repos or []:
        import shlex
        quoted = shlex.quote(repo)
        head, _, code = await executor.execute(f"git -C {quoted} rev-parse HEAD", timeout=10)
        if code != 0:
            continue
        branch, _, branch_code = await executor.execute(f"git -C {quoted} symbolic-ref --short -q HEAD", timeout=10)
        sha = head.strip()
        if branch_code == 0 and branch.strip():
            entry.inverse.append(f"git -C {quoted} checkout {shlex.quote(branch.strip())}")
        entry.inverse.append(f"git -C {quoted} reset --keep {shlex.quote(sha)}")
    if crontab:
        out, _, code = await executor.execute("crontab -l", timeout=10)
        entry.crontab = out if code == 0 and out != "Komenda wykonana bez outputu" else ""
    _save(entry)
    prune()
    return entry


def finish(entry: Entry, status: str, exit_code: int | None = None, verify: str = "") -> None:
    entry.status, entry.exit_code, entry.verify = status, exit_code, verify
    if _entry_dir(entry.id).is_dir():  # wpis usuniety przez prune() nie wraca bez kopii
        _save(entry)


# ─── Przywracanie ───────────────────────────────────────────────────────────

def restore_files(entry: Entry) -> list[str]:
    """Przywraca pliki z kopii. Zwraca opis kazdego kroku."""
    directory = _entry_dir(entry.id)
    report = []
    for backup in entry.files:
        if backup.skipped:
            report.append(f"{backup.label}: pominieto ({backup.skipped})")
            continue
        try:
            if not backup.existed:
                if os.path.lexists(backup.local) and not os.path.isdir(backup.local):
                    os.unlink(backup.local)
                    report.append(f"{backup.label}: usunieto (nie istnial przed zmiana)")
                else:
                    report.append(f"{backup.label}: bez zmian (nie istnial i nie istnieje)")
                continue
            os.makedirs(os.path.dirname(backup.local) or "/", exist_ok=True)
            tmp = backup.local + ".pipe-restore"
            shutil.copyfile(directory / backup.blob, tmp)
            if backup.mode is not None:
                os.chmod(tmp, backup.mode)
            if backup.uid is not None and os.geteuid() == 0:
                try:
                    os.chown(tmp, backup.uid, backup.gid if backup.gid is not None else -1)
                except OSError:
                    pass
            os.replace(tmp, backup.local)
            report.append(f"{backup.label}: przywrocono")
        except OSError as exc:
            report.append(f"{backup.label}: BLAD przywracania ({exc.strerror or exc})")
    return report


def preview(entry: Entry, max_lines: int = 30) -> str:
    """Co zrobi cofniecie — roznice plikow (obecny -> kopia) i komendy odwrotne."""
    import difflib

    from backend.core.text import as_code

    directory = _entry_dir(entry.id)
    parts = [f"Cofniecie {entry.summary()}"]
    for backup in entry.files:
        if backup.skipped:
            parts.append(f"- {backup.label}: brak kopii ({backup.skipped})")
            continue
        if not backup.existed:
            parts.append(f"- {backup.label}: zostanie usuniety (nie istnial przed zmiana)")
            continue
        try:
            old = (directory / backup.blob).read_text(encoding="utf-8", errors="replace")
            current = Path(backup.local).read_text(encoding="utf-8", errors="replace") \
                if os.path.isfile(backup.local) else ""
        except OSError:
            parts.append(f"- {backup.label}: zostanie przywrocony")
            continue
        if old == current:
            parts.append(f"- {backup.label}: bez roznic (tylko uprawnienia/wlasciciel, jesli sie zmienily)")
            continue
        diff = list(difflib.unified_diff(current.splitlines(), old.splitlines(), fromfile="obecny",
                                         tofile="po cofnieciu", lineterm=""))
        if len(diff) > max_lines:
            diff = diff[:max_lines] + [f"[... i {len(diff) - max_lines} linii roznicy]"]
        parts.append(f"- {backup.label}:\n" + as_code("\n".join(diff)))
    if entry.crontab is not None:
        parts.append("- crontab zostanie przywrocony do stanu sprzed zmiany")
    for command in entry.inverse:
        parts.append(f"- komenda odwrotna: {as_code(command)}")
    for note in entry.notes:
        parts.append(f"- uwaga: {note}")
    return "\n".join(parts)


async def rollback(entry: Entry, interface: str) -> str:
    """Cofa wpis: kopia stanu biezacego (nowy wpis), pliki z kopii, crontab, komendy odwrotne."""
    import shlex

    from backend.core import audit, executor
    from backend.core.security import classify_command

    if not entry.undoable:
        return f"Wpisu #{entry.id} nie da sie cofnac (status: {entry.status})."
    redo = await begin(interface, "cofnij", f"cofniecie #{entry.id}",
                       files=[(f.local, f.label) for f in entry.files if not f.skipped], undo_of=entry.id)
    lines = restore_files(entry)
    if entry.crontab is not None:
        path = _entry_dir(entry.id) / "crontab.txt"
        path.write_text(entry.crontab, encoding="utf-8")
        os.chmod(path, 0o600)
        out, err, code = await executor.execute(f"crontab {shlex.quote(str(path))}", timeout=20)
        lines.append("crontab: przywrocono" if code == 0 else f"crontab: BLAD ({(err or out).strip()[:200]})")
    failed = False
    for command in entry.inverse:
        if classify_command(command) == "forbidden":
            lines.append(f"{command}: ODMOWA — komenda zakazana, pominieto")
            await audit.log_blocked(interface, f"cofnij({command})")
            failed = True
            continue
        out, err, code = await executor.execute(command, timeout=300)
        await audit.log_confirmed(interface, f"cofnij #{entry.id}: {command}", code)
        tail = (err or out).strip().splitlines()[-1:] if code else []
        lines.append(f"{command}: " + ("ok" if code == 0 else f"BLAD (exit {code}) {tail[0][:200] if tail else ''}"))
        failed = failed or code != 0
    failed = failed or any("BLAD" in line for line in lines)
    entry.status = "rolled_back" if not failed else entry.status
    _save(entry)
    finish(redo, "done" if not failed else "failed", 0 if not failed else 1)
    for backup in entry.files:
        await audit.log_file_write(interface, f"cofnij #{entry.id}: {backup.label}", 0)
    head = f"Cofnieto #{entry.id}." if not failed else f"Cofniecie #{entry.id} czesciowo nieudane."
    return head + "\n" + "\n".join(f"- {line}" for line in lines) + \
        f"\nStan sprzed cofniecia zapisany jako #{redo.id} (mozna go przywrocic: /cofnij {redo.id})."


def render_list(limit: int = 15) -> str:
    found = entries(limit)
    if not found:
        return "Dziennik zmian jest pusty — zapisuje sie przy kazdej zatwierdzonej zmianie."
    return "\n".join(e.summary() for e in found)


def as_data(entry: Entry) -> dict[str, Any]:
    return {"id": entry.id, "summary": entry.summary(), "undoable": entry.undoable, "status": entry.status}
