"""
Tokeny klientow z rolami — osobny, odwolywalny token na laptop / osobe.

    python3 -m backend.tokens add laptop-kuba --role admin
    python3 -m backend.tokens add stazysta --role viewer
    python3 -m backend.tokens list
    python3 -m backend.tokens revoke stazysta

Role:
  admin   wszystko (zmiany z potwierdzeniem, /cofnij)
  viewer  tylko odczyty: rozmowa, diagnoza, raporty, wykresy; zadnych zmian ani potwierdzen

Token jest pokazywany raz; w DATA_DIR/tokens.json lezy tylko jego skrot SHA-256.
Obok dzialaja AGENT_TOKEN (admin) i AGENT_VIEWER_TOKEN (viewer) z backend/.env.
Modul uzywa tylko biblioteki standardowej (jak configure.py).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

from backend.core.i18n import load_env_lang, tr

ROLES = ("admin", "viewer")
_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,39}$")


def tokens_path() -> Path:
    custom = os.getenv("DATA_DIR", "").strip()
    base = Path(custom) if custom else Path(__file__).resolve().parent / "data"
    return base / "tokens.json"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def load() -> list[dict]:
    try:
        raw = json.loads(tokens_path().read_text(encoding="utf-8"))
        return [t for t in raw if isinstance(t, dict) and t.get("hash") and t.get("role") in ROLES]
    except (OSError, json.JSONDecodeError, TypeError):
        return []


def _save(entries: list[dict]) -> None:
    path = tokens_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(entries, indent=1) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def add(name: str, role: str) -> str:
    name = (name or "").strip().lower()
    if not _NAME.match(name):
        raise ValueError(tr("Nazwa: male litery, cyfry, '.', '-', '_' (do 40 znakow).",
                            "Name: lowercase letters, digits, '.', '-', '_' (up to 40 characters)."))
    if role not in ROLES:
        raise ValueError(tr(f"Rola: {' albo '.join(ROLES)}.", f"Role: {' or '.join(ROLES)}."))
    entries = [t for t in load() if t.get("name") != name]
    token = secrets.token_urlsafe(24)
    entries.append({"name": name, "role": role, "hash": _digest(token), "created": time.strftime("%Y-%m-%d %H:%M")})
    _save(entries)
    return token


def revoke(name: str) -> bool:
    entries = load()
    kept = [t for t in entries if t.get("name") != (name or "").strip().lower()]
    if len(kept) == len(entries):
        return False
    _save(kept)
    return True


def match(token: str) -> tuple[str, str] | None:
    """(nazwa, rola) dla tokenu albo None. Porownanie skrotow w stalym czasie."""
    if not token:
        return None
    digest = _digest(token)
    for entry in load():
        if hmac.compare_digest(entry["hash"], digest):
            return entry.get("name", "?"), entry["role"]
    return None


def main(argv: list[str] | None = None) -> int:
    load_env_lang(Path(__file__).resolve().parent / ".env")
    parser = argparse.ArgumentParser(prog="python3 -m backend.tokens", description=tr("Tokeny klientow Pipe z rolami.", "Pipe client tokens with roles."))
    sub = parser.add_subparsers(dest="cmd", required=True)
    add_p = sub.add_parser("add", help=tr("nowy token (pokazany raz)", "new token (shown once)"))
    add_p.add_argument("name")
    add_p.add_argument("--role", choices=ROLES, default="admin")
    sub.add_parser("list", help=tr("lista tokenow (bez wartosci)", "list tokens (without values)"))
    rev = sub.add_parser("revoke", help=tr("odwolaj token", "revoke a token"))
    rev.add_argument("name")
    args = parser.parse_args(argv)
    if args.cmd == "add":
        try:
            token = add(args.name, args.role)
        except ValueError as exc:
            print(tr(f"Blad: {exc}", f"Error: {exc}"), file=sys.stderr)
            return 1
        print(tr(f"Token dla {args.name} (rola {args.role}) — zapisz go teraz, nie da sie go odczytac pozniej:\n{token}",
                 f"Token for {args.name} (role {args.role}) — save it now, it cannot be read later:\n{token}"))
        return 0
    if args.cmd == "list":
        entries = load()
        for entry in entries:
            print(f"{entry['name']:<24} {entry['role']:<7} {tr('utworzony', 'created')} {entry.get('created', '?')}")
        if not entries:
            print(tr("(brak tokenow — dziala AGENT_TOKEN / AGENT_VIEWER_TOKEN z .env)",
                     "(no tokens — AGENT_TOKEN / AGENT_VIEWER_TOKEN from .env apply)"))
        return 0
    if revoke(args.name):
        print(tr(f"Odwolano token {args.name}.", f"Revoked token {args.name}."))
        return 0
    print(tr(f"Nie ma tokenu {args.name}.", f"No token {args.name}."), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
