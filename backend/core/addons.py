"""
Katalog gotowych dodatkow MCP (backend/addons/*.json) — integracje, ktore uzytkownik wlacza jednym kliknieciem
w oknie `/mcp` (CLI, pipe web, Telegram). Nic nie jest wlaczone z gory: kazdy dodatek wymaga konta albo klucza
uzytkownika, a kazdy wlaczony serwer to kontener i narzedzia w prompcie.

Plik dodatku opisuje `backend/addons/README.md`. Konfiguracja serwera przechodzi ta sama walidacje co `/mcp dodaj`
(`registry.validate_server`: npx/uvx/obraz w izolowanym kontenerze, sekrety tylko `${MCP_*}`), a kazdy sekret,
do ktorego sie odwoluje, musi byc opisany w `secrets` — uzytkownik wpisuje go w oknie `/mcp`, nie w rozmowie.

Bez importu `settings`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.core.i18n import lang, tr
from backend.core.mcp import registry

ADDONS_DIR = Path(__file__).resolve().parents[1] / "addons"
CATEGORIES = ("office", "dev", "other")
_URL = re.compile(r"^https://[^\s]+$")


class AddonError(ValueError):
    """Bledny plik dodatku albo nieznany dodatek."""


@dataclass
class Secret:
    name: str
    label: dict[str, str]
    help: dict[str, str]
    url: str = ""


@dataclass
class Addon:
    name: str
    title: dict[str, str]
    description: dict[str, str]
    server: dict[str, Any]
    secrets: list[Secret] = field(default_factory=list)
    login: dict[str, str] = field(default_factory=dict)
    category: str = "other"
    homepage: str = ""

    def text(self, value: dict[str, str]) -> str:
        return value.get(lang()) or value.get("en") or value.get("pl") or ""

    def to_data(self, installed: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        """Dla klientow: bez wartosci sekretow, tylko czy sa ustawione."""
        installed = installed if installed is not None else registry.load_config()
        return {
            "name": self.name, "title": self.text(self.title), "description": self.text(self.description),
            "category": self.category, "homepage": self.homepage, "login": self.text(self.login),
            "installed": self.name in installed,
            "secrets": [{"name": s.name, "label": self.text(s.label), "help": self.text(s.help), "url": s.url,
                         "set": registry.has_secret(s.name)} for s in self.secrets],
            "ready": all(registry.has_secret(s.name) for s in self.secrets),
            "runs": registry.describe_server(self.name, registry.validate_server(self.name, self.server)),
        }


def _texts(raw: Any, field_name: str, name: str, required: bool = True) -> dict[str, str]:
    if not raw and not required:
        return {}
    if not isinstance(raw, dict) or not all(isinstance(raw.get(k), str) and raw.get(k).strip() for k in ("pl", "en")):
        raise AddonError(f"{name}: {field_name} needs non-empty 'pl' and 'en' texts")
    return {"pl": raw["pl"].strip(), "en": raw["en"].strip()}


def parse(raw: Any, filename: str = "") -> Addon:
    """Plik dodatku -> Addon. Rzuca AddonError (z nazwa pliku) przy kazdej niezgodnosci."""
    if not isinstance(raw, dict):
        raise AddonError(f"{filename}: expected a JSON object")
    name = str(raw.get("name", ""))
    if filename and Path(filename).stem != name:
        raise AddonError(f"{filename}: file name must be <name>.json")
    try:
        server = registry.validate_server(name, dict(raw.get("server") or {}))
    except registry.McpConfigError as exc:
        raise AddonError(f"{name}: {exc}") from None
    secrets = []
    for entry in raw.get("secrets") or []:
        if not isinstance(entry, dict) or not str(entry.get("name", "")).startswith("MCP_"):
            raise AddonError(f"{name}: each secret needs a name starting with MCP_")
        url = str(entry.get("url", ""))
        if url and not _URL.match(url):
            raise AddonError(f"{name}: secret url must be https")
        secrets.append(Secret(entry["name"], _texts(entry.get("label"), "label", name),
                              _texts(entry.get("help"), "help", name), url))
    declared = {s.name for s in secrets}
    missing = [ref for ref in registry.secret_refs(server) if ref not in declared]
    if missing:
        raise AddonError(f"{name}: describe these secrets in 'secrets': {', '.join(missing)}")
    category = str(raw.get("category", "other"))
    if category not in CATEGORIES:
        raise AddonError(f"{name}: category must be one of {', '.join(CATEGORIES)}")
    homepage = str(raw.get("homepage", ""))
    if homepage and not _URL.match(homepage):
        raise AddonError(f"{name}: homepage must be https")
    return Addon(name, _texts(raw.get("title"), "title", name), _texts(raw.get("description"), "description", name),
                 dict(raw["server"]), secrets, _texts(raw.get("login"), "login", name, required=False), category, homepage)


def catalog(directory: Path | None = None) -> list[Addon]:
    """Wszystkie dodatki (biuro, potem programisci, potem reszta; alfabetycznie). Bledny plik jest pomijany."""
    found = []
    for path in sorted((directory or ADDONS_DIR).glob("*.json")):
        try:
            found.append(parse(json.loads(path.read_text(encoding="utf-8")), path.name))
        except (OSError, ValueError) as exc:
            print(f"[MCP] {tr('Pomijam dodatek', 'Skipping add-on')} {path.name}: {exc}", flush=True)
    return sorted(found, key=lambda a: (CATEGORIES.index(a.category), a.name))


def get(name: str) -> Addon | None:
    name = (name or "").strip().lower()
    return next((a for a in catalog() if a.name == name), None)


def install(addon: Addon) -> bool:
    """Dodaje serwer dodatku do mcp_servers.json (jak /mcp dodaj). Zwraca, czy nowy."""
    missing = [s.name for s in addon.secrets if not registry.has_secret(s.name)]
    if missing:
        raise AddonError(tr(f"Najpierw wpisz: {', '.join(missing)} (okno /mcp w pipe web albo CLI).",
                            f"Enter these first: {', '.join(missing)} (the /mcp window in pipe web or the CLI)."))
    return registry.add_server(addon.name, {**addon.server, "description": addon.text(addon.description),
                                            "source": tr("katalog Pipe", "Pipe catalog"), "addon": addon.name})
