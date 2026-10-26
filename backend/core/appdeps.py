"""
Zaleznosci aplikacji w /zalataj — Pipe sam podbija podatne pakiety w plikach zaleznosci (core/patching.py, czesc `apps`).

Skan (core/vulns.py, cele `app`) mowi, ktore pakiety w lockfile'ach maja poprawke. Tu powstaje plan: dla kazdego
katalogu aplikacji menedzer, ktory umie zmienic sam plik blokady (bez instalowania pakietow), i lista
`(pakiet, wersja z poprawka)`. Menedzer dziala w jednorazowym kontenerze z oficjalnego obrazu, tak jak `exec`
w warsztacie (core/projects.py): widzi tylko katalog aplikacji (/work), bez docker.sock i uprawnien, jako wlasciciel
katalogu (pliki nie zmieniaja wlasciciela), skrypty instalacyjne pakietow wylaczone.

  npm      package-lock.json: zaleznosci z package.json -> `npm install <p>@<wersja> --package-lock-only`,
           przechodnie -> `npm audit fix --package-lock-only` (w zakresach wersji, bez --force)
  yarn     yarn.lock (yarn 2+): `yarn up` / `yarn up -R`, --mode=update-lockfile; yarn 1 nie umie zmienic samego
           pliku blokady — zostaje w raporcie
  pnpm     pnpm-lock.yaml: `pnpm update --lockfile-only` (bezposrednie z wersja, przechodnie --depth Infinity)
  pip      requirements*.txt z `==`: nowa wersja w tej samej linii (plik z --hash zostaje — hashe by nie pasowaly)
  poetry   poetry.lock: `poetry update --lock <pakiety>` (w ograniczeniach z pyproject.toml)
  uv       uv.lock: `uv lock --upgrade-package <p>==<wersja>`
  go       go.mod: `go get <modul>@v<wersja>` + `go mod tidy` (stdlib to wersja Go — zostaje w raporcie)
  cargo    Cargo.lock: `cargo update -p <p> --precise <wersja>`
  composer composer.lock: `composer update <p> --with-dependencies --no-install --no-scripts --no-plugins`
  bundler  Gemfile.lock: `bundle lock --update <p>`

Nazwy i wersje pochodza z plikow aplikacji, wiec kazda jest sprawdzana wyrazeniem dla swojego ekosystemu i trafia
do komendy jako jeden argument (shlex.quote). Czego menedzer nie podbil (ograniczenie wersji w manifescie), pokaze
ponowny skan — /zalataj nie probuje tego znowu przez dobe (vulns.HOLD_FOR), a raport mowi, co zmienic recznie.
Bez importu `settings`.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

from backend.core import runtime
from backend.core.i18n import tr

MEMORY = "2g"                # menedzery pakietow potrafia potrzebowac wiecej niz domyslny 1g warsztatu
CPUS = "2"
PIDS = 512

NODE_IMAGE = "node:22-bookworm-slim"
PYTHON_IMAGE = "python:3.12-slim"
IMAGES = {"npm": NODE_IMAGE, "yarn": NODE_IMAGE, "pnpm": NODE_IMAGE, "pip": PYTHON_IMAGE, "poetry": PYTHON_IMAGE,
          "uv": PYTHON_IMAGE, "go": "golang:1", "cargo": "rust:1-slim", "composer": "composer:2",
          "bundler": "ruby:3.3-slim"}

# Pliki, ktore menedzer moze zmienic — kopia w dzienniku przed zmiana (dla /cofnij)
TOUCHED = {"npm": ("package.json", "package-lock.json", "npm-shrinkwrap.json"), "yarn": ("package.json", "yarn.lock"),
           "pnpm": ("package.json", "pnpm-lock.yaml"), "poetry": ("pyproject.toml", "poetry.lock"),
           "uv": ("pyproject.toml", "uv.lock"), "go": ("go.mod", "go.sum"), "cargo": ("Cargo.lock",),
           "composer": ("composer.json", "composer.lock"), "bundler": ("Gemfile", "Gemfile.lock")}

NAMES = {
    "npm": re.compile(r"^(@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*$", re.I),
    "PyPI": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
    "Go": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~/-]*$"),
    "crates.io": re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$"),
    "Packagist": re.compile(r"^[a-z0-9][a-z0-9_.-]*/[a-z0-9][a-z0-9_.-]*$"),
    "RubyGems": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
}
VERSION = re.compile(r"^v?[0-9][0-9A-Za-z.+_~-]{0,63}$")
_PIP_NAME = re.compile(r"[-_.]+")


@dataclass
class Bump:
    manager: str
    packages: list[tuple[str, str]] = field(default_factory=list)   # (nazwa, wersja z poprawka)
    direct: set[str] = field(default_factory=set)                   # npm/yarn/pnpm: zaleznosci z package.json
    exact: set[str] = field(default_factory=set)                    # ...przypiete dokladnie ("4.17.20") — zostaja dokladne
    files: list[str] = field(default_factory=list)                  # requirements*.txt (pip)


@dataclass
class AppBump:
    """Jeden katalog aplikacji: co podbic i czym."""
    directory: str                                  # sciezka hosta
    target: Any                                     # vulns.TargetResult
    bumps: list[Bump] = field(default_factory=list)
    items: list = field(default_factory=list)      # vulns.PackageVulns, ktore podbijamy
    manual: list[str] = field(default_factory=list)   # czego nie umiem podbic i dlaczego

    @property
    def files(self) -> list[str]:
        """Sciezki hosta plikow, ktore moga sie zmienic (do kopii w dzienniku)."""
        names: list[str] = []
        for bump in self.bumps:
            names += [os.path.basename(f) for f in bump.files] if bump.manager == "pip" else list(TOUCHED[bump.manager])
        paths = [os.path.join(self.directory, n) for n in dict.fromkeys(names)]
        return [p for p in paths if os.path.isfile(runtime.to_local(p))]


def app_key(target: Any, item: Any) -> str:
    """Klucz wstrzymania pakietu aplikacji — ten sam pakiet w innej aplikacji to co innego."""
    return f"app:{target.name}|{item.key}"


def _read(path: str) -> str:
    try:
        with open(runtime.to_local(path), encoding="utf-8", errors="replace") as fh:
            return fh.read(5 * 1024 * 1024)
    except OSError:
        return ""


_EXACT = re.compile(r"^=?v?\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?$")


def _direct_npm(directory: str) -> dict[str, bool]:
    """Zaleznosci z package.json -> czy przypiete dokladnie (bez ^, ~, zakresu)."""
    try:
        data = json.loads(_read(os.path.join(directory, "package.json")) or "{}")
    except ValueError:
        return {}
    names: dict[str, bool] = {}
    for key in ("dependencies", "devDependencies", "optionalDependencies"):
        if isinstance(data.get(key), dict):
            names.update({str(n): bool(_EXACT.match(str(spec).strip())) for n, spec in data[key].items()})
    return names


def managers(files: list[str]) -> tuple[dict[str, list[str]], list[str]]:
    """
    Pliki zaleznosci katalogu -> {ekosystem: [menedzery]} i powody, dla ktorych czegos nie da sie podbic.
    Dla npm wygrywa pierwszy plik blokady (package-lock, potem yarn, potem pnpm) — tak jak robi to sam projekt.
    """
    names = {os.path.basename(f): f for f in files}
    found: dict[str, list[str]] = {}
    manual: list[str] = []
    if "package-lock.json" in names or "npm-shrinkwrap.json" in names:
        found["npm"] = ["npm"]
    elif "yarn.lock" in names:
        if "__metadata:" in _read(names["yarn.lock"]):
            found["npm"] = ["yarn"]
        else:
            manual.append(tr("yarn 1 nie umie zmienic samego yarn.lock — uruchom `yarn upgrade <pakiet>` w aplikacji",
                             "yarn 1 cannot change yarn.lock alone — run `yarn upgrade <package>` in the app"))
    elif "pnpm-lock.yaml" in names:
        found["npm"] = ["pnpm"]
    python = []
    if any(re.match(r"^requirements([-_.][\w.-]+)?\.txt$", n) for n in names):
        python.append("pip")
    if "poetry.lock" in names:
        python.append("poetry")
    if "uv.lock" in names:
        python.append("uv")
    if python:
        found["PyPI"] = python
    elif "Pipfile.lock" in names:
        manual.append(tr("Pipfile.lock: uruchom `pipenv update <pakiet>` w aplikacji",
                         "Pipfile.lock: run `pipenv update <package>` in the app"))
    for name, eco, manager in (("go.mod", "Go", "go"), ("Cargo.lock", "crates.io", "cargo"),
                               ("composer.lock", "Packagist", "composer"), ("Gemfile.lock", "RubyGems", "bundler")):
        if name in names:
            found[eco] = [manager]
    return found, manual


def _version(eco: str, version: str) -> str:
    version = version.strip()
    if eco == "Go" and version and not version.startswith("v"):
        version = "v" + version
    return version if VERSION.match(version) else ""


def plan_target(target: Any, held: dict | set = frozenset()) -> AppBump:
    """Plan dla jednego katalogu aplikacji (vulns.TargetResult kind 'app'); `held` — klucze wstrzymane (app_key)."""
    directory = str(target.name)
    bump = AppBump(directory, target)
    by_eco, bump.manual = managers(list(target.meta.get("files") or []))
    direct = _direct_npm(directory) if "npm" in by_eco else {}
    planned: dict[str, Bump] = {}
    for item in target.items:
        if app_key(target, item) in held:
            continue
        eco, fixed = item.ecosystem, _version(item.ecosystem, item.fixed_version)
        pattern = NAMES.get(eco)
        if eco not in by_eco or not fixed or not pattern or not pattern.match(item.name):
            continue
        if eco == "Go" and item.name in ("stdlib", "toolchain"):
            bump.manual.append(tr(f"Go {item.version} (stdlib): podbij wersje Go w go.mod (`go` / `toolchain`) i "
                                  "przebuduj aplikacje", f"Go {item.version} (stdlib): bump the Go version in go.mod "
                                  "(`go` / `toolchain`) and rebuild the app"))
            continue
        for manager in by_eco[eco]:
            entry = planned.setdefault(manager, Bump(manager))
            entry.packages.append((item.name, fixed))
            if manager in ("npm", "yarn", "pnpm") and item.name in direct:
                entry.direct.add(item.name)
                if direct[item.name]:
                    entry.exact.add(item.name)
        bump.items.append(item)
    if "pip" in planned:
        planned["pip"].files = [f for f in target.meta.get("files") or []
                                if re.match(r"^requirements([-_.][\w.-]+)?\.txt$", os.path.basename(f))
                                and "--hash" not in _read(f)]
        if not planned["pip"].files:
            del planned["pip"]
            bump.manual.append(tr("requirements z --hash: nowe hashe trzeba wygenerowac (pip-compile)",
                                  "requirements with --hash: new hashes have to be generated (pip-compile)"))
    bump.bumps = list(planned.values())
    if not bump.bumps:
        bump.items = []
    bump.manual = list(dict.fromkeys(bump.manual))
    return bump


def plan(scan: Any, held: dict | set = frozenset()) -> list[AppBump]:
    """Katalogi aplikacji z czymkolwiek do podbicia (w kolejnosci od najgrozniejszej podatnosci)."""
    from backend.core import vulns

    found = [plan_target(t, held) for t in sorted((t for t in scan.of_kind("app") if t.items), key=vulns.target_rank)]
    return [b for b in found if b.bumps and vulns._HOST_PATH.match(b.directory)]


# ─── Komendy ────────────────────────────────────────────────────────────────

def _q(values) -> str:
    return " ".join(shlex.quote(v) for v in values)


def _each(commands: list[str]) -> str:
    """Kazda komenda niezaleznie (jedna nieudana nie zatrzymuje reszty); kod wyjscia != 0, gdy ktoras zawiodla."""
    return "rc=0; " + " ".join(f"{c} || rc=1;" for c in commands) + " exit $rc"


# Edycja requirements*.txt w kontenerze Pythona: ta sama linia, nowa wersja po `==` (reszta linii bez zmian).
_PIP_EDIT = r'''
import json, os, re
bumps = {re.sub(r"[-_.]+", "-", k.lower()): v for k, v in json.loads(os.environ["PIPE_BUMPS"]).items()}
line_re = re.compile(r"^(\s*)([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?(\s*)(===?)(\s*)([A-Za-z0-9._+!-]+)(.*)$")
for name in json.loads(os.environ["PIPE_FILES"]):
    with open(name, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    changed = False
    for i, line in enumerate(lines):
        m = line_re.match(line)
        new = m and bumps.get(re.sub(r"[-_.]+", "-", m.group(2).lower()))
        if new and new != m.group(7):
            lines[i] = "".join(g or "" for g in m.groups()[:6]) + new + m.group(8)
            changed = True
    if changed:
        with open(name, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        print("zmieniono " + name)
'''


def script(bump: Bump) -> tuple[str, dict[str, str]]:
    """(komenda sh, zmienne srodowiska) menedzera w kontenerze; katalog aplikacji to /work."""
    direct = [(n, v) for n, v in bump.packages if n in bump.direct]
    indirect = [n for n, _ in bump.packages if n not in bump.direct]
    env: dict[str, str] = {}
    if bump.manager == "npm":
        steps = []
        for exact, flag in ((False, ""), (True, "--save-exact ")):
            chosen = [(n, v) for n, v in direct if (n in bump.exact) == exact]
            if chosen:
                steps.append(f"npm install --package-lock-only --ignore-scripts --no-audit --no-fund --loglevel=error {flag}"
                             + _q(f"{n}@{v}" for n, v in chosen))
        if indirect:
            # audit fix zwraca blad, gdy cos zostalo (np. poza zakresem wersji) — to pokaze ponowny skan
            steps.append("{ npm audit fix --package-lock-only --ignore-scripts --no-fund --loglevel=error || true; }")
        return " && ".join(steps), env
    if bump.manager == "yarn":
        env = {"YARN_ENABLE_SCRIPTS": "0", "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0", "COREPACK_HOME": "/tmp/corepack"}
        steps = []
        if direct:
            steps.append("corepack yarn up --mode=update-lockfile "
                         + _q(f"{n}@{v}" if n in bump.exact else f"{n}@^{v}" for n, v in direct))
        if indirect:
            steps.append("corepack yarn up -R --mode=update-lockfile " + _q(indirect))
        return _each(steps), env
    if bump.manager == "pnpm":
        env = {"COREPACK_ENABLE_DOWNLOAD_PROMPT": "0", "COREPACK_HOME": "/tmp/corepack"}
        steps = []
        if direct:
            steps.append("corepack pnpm update --lockfile-only " + _q(f"{n}@{v}" for n, v in direct))
        if indirect:
            steps.append("corepack pnpm update --lockfile-only --depth Infinity " + _q(indirect))
        return _each(steps), env
    if bump.manager == "pip":
        env = {"PIPE_BUMPS": json.dumps(dict(bump.packages)),
               "PIPE_FILES": json.dumps([os.path.basename(f) for f in bump.files]), "PIPE_EDIT": _PIP_EDIT}
        return 'python3 -c "$PIPE_EDIT"', env
    if bump.manager == "poetry":
        return ("python3 -m venv /tmp/v && /tmp/v/bin/pip install -q --disable-pip-version-check poetry && "
                "/tmp/v/bin/poetry update --lock --no-interaction " + _q(n for n, _ in bump.packages)), env
    if bump.manager == "uv":
        return ("python3 -m venv /tmp/v && /tmp/v/bin/pip install -q --disable-pip-version-check uv && /tmp/v/bin/uv lock "
                + " ".join(f"--upgrade-package {shlex.quote(f'{n}=={v}')}" for n, v in bump.packages)), env
    if bump.manager == "go":
        env = {"GOFLAGS": "-mod=mod", "GOPATH": "/tmp/go", "GOCACHE": "/tmp/gocache", "GOTOOLCHAIN": "auto"}
        return "go get " + _q(f"{n}@{v}" for n, v in bump.packages) + " && go mod tidy", env
    if bump.manager == "cargo":
        env = {"CARGO_HOME": "/tmp/cargo"}
        return _each([f"cargo update -p {shlex.quote(n)} --precise {shlex.quote(v)}" for n, v in bump.packages]), env
    if bump.manager == "composer":
        env = {"COMPOSER_HOME": "/tmp/composer"}
        return ("composer update --with-dependencies --no-install --no-scripts --no-plugins --no-interaction "
                "--ignore-platform-reqs " + _q(n for n, _ in bump.packages)), env
    if bump.manager == "bundler":
        env = {"BUNDLE_USER_HOME": "/tmp/bundle", "GEM_HOME": "/tmp/gems"}
        return "bundle lock --update " + _q(n for n, _ in bump.packages), env
    raise ValueError(bump.manager)


def owner(directory: str) -> tuple[int, int]:
    """Wlasciciel katalogu aplikacji — menedzer dziala jako on, wiec pliki zostaja jego."""
    try:
        stat = os.stat(runtime.to_local(directory))
        return stat.st_uid, stat.st_gid
    except OSError:
        return 0, 0


def docker_command(directory: str, bump: Bump, uid: int, gid: int) -> str:
    """`docker run` jednorazowego kontenera menedzera: tylko katalog aplikacji, bez uprawnien i bez docker.sock."""
    command, env = script(bump)
    env = {"HOME": "/tmp", "CI": "1", "npm_config_update_notifier": "false", **env}
    argv = ["docker", "run", "--rm", "--init", "--label", "pipe.appdeps=1", "--network", "bridge", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", str(PIDS), "--memory", MEMORY,
            "--memory-swap", MEMORY, "--cpus", CPUS, "--user", f"{int(uid)}:{int(gid)}"]
    for key, value in env.items():
        argv += ["-e", f"{key}={value}"]
    argv += ["-v", f"{directory}:/work", "-w", "/work", IMAGES[bump.manager], "sh", "-c", command]
    return _q(argv)


def describe(bump: AppBump) -> str:
    """'shop (npm: 3, PyPI: 1)' — do postepu i wyniku."""
    counts: dict[str, int] = {}
    for item in bump.items:
        counts[item.ecosystem] = counts.get(item.ecosystem, 0) + 1
    return f"{bump.directory} (" + ", ".join(f"{e}: {n}" for e, n in counts.items()) + ")"
