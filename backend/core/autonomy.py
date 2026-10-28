"""
Samodzielnosc — o co Pipe pyta, a co robi sam.

Komenda, ktora klasyfikator (core/security.py) zostawia do potwierdzenia, zapis pliku i operacje narzedzi trafiaja
tu, zanim uzytkownik dostanie pytanie. Rutynowa zmiana wykonuje sie od razu przez bezpiecznik (core/safety.py):
kopia plikow do dziennika, sprawdzenie przed, weryfikacja po, wpis, ktory /cofnij odwraca. Rutyna to restart
i przeladowanie uslug i kontenerow, `docker compose up -d` / `pull`, edycja konfiguracji (z kopia), instalacja
i aktualizacja pakietow z repozytorium systemu, `git pull` w katalogu aplikacji, sprzatanie cache i starych logow.

Pytanie zostaje przy sprawach waznych (`reason()` zwraca powod):

  - dane, ktorych bezpiecznik nie skopiuje: rm -r, pliki wieksze niz kopia dziennika, wolumeny, kontenery,
    git reset --hard / clean, usuwanie pakietow,
  - dostep i autostart: SSH, uzytkownicy, sudo, PAM, zapora, cron, systemd, pliki startowe powloki, repozytoria
    pakietow, uprawnienia setuid i zapis dla wszystkich,
  - dane wychodzace z serwera (curl/wget do obcych, git push) i pliki z sekretami,
  - kod: skrypty i pliki programow, pip/npm install, docker run/exec, kubectl apply,
  - odciecie dostepu do serwera albo samego Pipe (ssh, docker, siec, wlasny kontener), restart maszyny,
  - zdalne serwery: tam zmiana plikow nie ma kopii po stronie Pipe.

Komenda, ktorej Pipe nie rozpoznaje, zawsze czeka na TAK: Pipe czyta logi i strony, w ktorych obcy moze zostawic
tekst udajacy polecenie, wiec sam wykonuje tylko to, co zna. Decyzja jest mechaniczna — leksyka komendy i sciezki,
bez pytania modelu. Bez importu `settings`: wylacznik (AUTONOMY) i rola sprawdza Session.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field

from backend.core import journal, runtime, security
from backend.core.i18n import tr

# ─── Powody pytania ─────────────────────────────────────────────────────────


def _why(kind: str) -> str:
    return tr(*{
        "unknown": ("komenda, ktorej nie rozpoznaje", "a command I do not recognise"),
        "dynamic": ("komenda z podstawieniem powloki — nie wiem z gory, co sie wykona",
                    "a command with shell substitution — I cannot tell in advance what runs"),
        "secret": ("dotyka pliku z sekretami", "it touches a file with secrets"),
        "delete": ("usuwa dane, ktorych nie skopiuje", "it deletes data I cannot back up"),
        "access": ("zmienia dostep do serwera (SSH, uzytkownicy, sudo, zapora, uprawnienia)",
                   "it changes access to the server (SSH, users, sudo, firewall, permissions)"),
        "startup": ("dodaje cos, co uruchamia sie samo (cron, systemd, pliki startowe)",
                    "it adds something that runs by itself (cron, systemd, startup files)"),
        "system": ("zmienia pliki systemu albo jego zrodla pakietow", "it changes system files or package sources"),
        "egress": ("wysyla albo pobiera dane spoza serwera", "it sends or fetches data outside the server"),
        "code": ("uruchamia albo zapisuje kod", "it runs or writes code"),
        "outage": ("moze odciac dostep do serwera albo zatrzymac Pipe",
                   "it can cut access to the server or stop Pipe"),
        "reboot": ("restart albo wylaczenie maszyny", "it restarts or shuts down the machine"),
        "process": ("zatrzymuje procesy", "it stops processes"),
        "remote": ("zmiana plikow na zdalnym serwerze — bez kopii po stronie Pipe",
                   "it changes files on a remote server — no backup on the Pipe side"),
        "container": ("daje kontenerowi dostep do hosta", "it gives a container access to the host"),
        "public": ("pliki w katalogu strony — widac je z internetu", "files in a site directory — visible from the internet"),
        "expose": ("moze udostepnic katalogi serwera w internecie (alias/root/autoindex)",
                   "it can expose server directories on the internet (alias/root/autoindex)"),
        "pipe": ("dane albo ustawienia samego Pipe", "Pipe's own data or settings"),
        "tool": ("operacja, o ktora zawsze pytam", "an operation I always ask about"),
    }[kind])


# ─── Sciezki ────────────────────────────────────────────────────────────────

# Pliki uruchamiane same (logowanie, start, cron, systemd) — zapis moze zalozyc trwaly dostep do serwera.
# Ta sama lista ostrzega w potwierdzeniu write_file (handlers/file_ops.py).
STARTUP_PATTERNS = (
    r"/\.(bashrc|bash_profile|profile|zshrc|zprofile|zshenv|bash_login|bash_logout)$",
    r"/\.ssh/(authorized_keys|config)$",
    r"/\.config/systemd/", r"/etc/systemd/", r"/\.gitconfig$", r"/\.git/hooks/",
    r"/etc/cron", r"/var/spool/cron", r"/etc/profile\.d/", r"/etc/rc\.local$",
    r"/\.(profile|bashrc)\.d/",
)
_STARTUP = [re.compile(p) for p in STARTUP_PATTERNS + (
    r"^/etc/(init\.d|rc\d?\.d|update-motd\.d|logrotate\.d)/", r"^/etc/logrotate\.conf$", r"^/etc/(profile|bash\.bashrc|environment)$",
    r"^/etc/zsh/", r"^/lib/systemd/", r"^/usr/lib/systemd/", r"/\.git/",
)]
_ACCESS = [re.compile(p) for p in (
    r"/\.ssh(/|$)", r"^/etc/ssh/", r"^/etc/sudoers", r"^/etc/pam\.d/", r"^/etc/security/",
    r"^/etc/(passwd|group|shadow|gshadow|subuid|subgid)-?$", r"^/etc/(ufw|iptables|nftables|firewalld)(/|\.|$)",
    r"^/etc/hosts\.(allow|deny)$", r"^/etc/polkit", r"^/etc/letsencrypt/", r"^/etc/ssl/private/",
)]
_SYSTEM = [re.compile(p) for p in (
    r"^/(usr|bin|sbin|lib|lib32|lib64|libx32|boot|proc|sys|dev)(/|$)", r"^/etc/ld\.so\.", r"^/etc/(fstab|crypttab)$",
    r"^/etc/default/grub", r"^/etc/(modprobe\.d|modules|udev|sysctl)", r"^/etc/apt/", r"^/etc/yum\.repos\.d/",
    r"^/etc/apk/", r"^/etc/docker/", r"^/var/lib/docker/", r"^/etc/(containerd|systemd-networkd|netplan|network)/",
)]
# Pliki kodu: zapis to wykonanie (skrypt CGI/PHP w katalogu strony, skrypt uruchamiany przez usluge).
_CODE = re.compile(r"(\.(php\d?|phtml|phar|jsp|jspx|aspx?|cgi|fcgi|pl|py|rb|sh|bash|zsh|js|mjs|cjs|ts|lua|so)"
                   r"|/\.htaccess|/\.user\.ini)$", re.IGNORECASE)
# Sekrety (tez do zapisu: .env aplikacji i Pipe trzyma klucze, adres providera LLM, tokeny)
_SECRET = re.compile(r"(^|/)\.env(\.[\w.-]+)?$|\.(pem|key|p12|pfx)$|/tokens\.json$|/llm_keys\.json$")
# Opcje `docker run`/`create` dajace kontenerowi hosta (osobny powod pytania, mocniejszy niz "kod")
_RISKY_CONTAINER = re.compile(r"privileged|docker\.sock|--cap-add|--security-opt|--device|--(pid|ipc|userns|network)[= ]host"
                              r"|[\"'\s-]/:/", re.IGNORECASE)
# Pliki compose i Dockerfile mowia, jaki kod i z jakimi montowaniami uruchomi `docker compose up -d` (rutyna) —
# ich zmiana to zmiana kodu, nie konfiguracji
_CONTAINER_DEF = re.compile(r"(^|/)((docker-)?compose[\w.-]*\.ya?ml|Dockerfile[\w.-]*|[\w.-]+\.dockerfile|Containerfile)$",
                            re.IGNORECASE)
# Katalogi stron: plik tu zapisany albo skopiowany widac z internetu (wyciek danych zamiast eksfiltracji siecia)
_WEB_ROOT = re.compile(r"^/(var|srv)/www(/|$)|/(public_html|htdocs|html|www|wwwroot|public|static|uploads)(/|$)")
# Konfiguracja serwera WWW: dyrektywy, ktore wystawiaja katalogi, sprawdzane w nowej tresci
_WEB_CONF = re.compile(r"^/etc/(nginx|apache2|httpd|caddy|haproxy|lighttpd)/|(^|/)Caddyfile$")
_EXPOSE = re.compile(r"\b(alias|root|autoindex|documentroot|file_server|indexes|browse)\b", re.IGNORECASE)


@dataclass
class Context:
    """Co Pipe wie o otoczeniu w chwili decyzji (zbiera agent; tu nic nie jest odczytywane z sieci)."""
    cwd: str = "/"
    remote: bool = False                                    # komenda na zdalnym celu (ssh/docker exec/kubectl)
    own_project: str = ""                                   # projekt compose kontenera Pipe
    own_names: set[str] = field(default_factory=set)        # nazwy/id kontenera Pipe
    own_hosts: set[str] = field(default_factory=set)        # domeny, ktore obsluguje ten serwer (curl do nich)
    protected: tuple[str, ...] = ()                         # katalogi danych Pipe (sciezki hosta)


# Katalogi najwyzszego poziomu: chmod -R / chown -R / rm na nich to zmiana calego systemu
_TOP_DIRS = {"/", "/etc", "/var", "/home", "/root", "/opt", "/srv", "/tmp", "/var/lib", "/var/log", "/mnt", "/media",
             "/run"}


def path_reason(host_path: str, ctx: Context | None = None, content: str | None = None) -> str:
    """Dlaczego zmiana pliku (sciezka hosta) czeka na TAK ('' — rutyna)."""
    path = os.path.normpath(host_path or "/")
    if path in _TOP_DIRS:
        return _why("system")
    protected = ctx.protected if ctx else ()
    if any(path == p or path.startswith(p.rstrip("/") + "/") for p in protected if p) \
            or re.search(r"(^|/)backend/data(/|$)", path):
        return _why("pipe")
    if _SECRET.search(path):
        return _why("secret")
    if any(p.search(path) for p in _ACCESS):
        return _why("access")
    if any(p.search(path) for p in _STARTUP):
        return _why("startup")
    if any(p.search(path) for p in _SYSTEM):
        return _why("system")
    if _CODE.search(path) or _CONTAINER_DEF.search(path):
        return _why("code")
    if _WEB_ROOT.search(path):
        return _why("public")
    if content is not None and _WEB_CONF.search(path) and any(_EXPOSE.search(line) for line in _added_lines(path, content)):
        return _why("expose")
    return ""


def _added_lines(host_path: str, content: str) -> list[str]:
    """Linie nowej tresci, ktorych nie ma w obecnym pliku (caly plik, gdy go nie ma albo nie da sie przeczytac)."""
    try:
        with open(runtime.to_local(host_path), encoding="utf-8", errors="replace") as fh:
            old = {line.strip() for line in fh}
    except OSError:
        old = set()
    return [line for line in content.splitlines() if line.strip() and line.strip() not in old]


def _too_big(host_path: str) -> bool:
    """Plik albo katalog, ktorego dziennik nie skopiuje (wiekszy niz journal.MAX_FILE_BYTES)."""
    local = runtime.to_local(host_path)
    try:
        if os.path.isdir(local) and not os.path.islink(local):
            return True
        return os.path.getsize(local) > journal.MAX_FILE_BYTES
    except OSError:
        return False                                        # pliku nie ma — nic nie ginie


def _host(path: str, ctx: Context) -> str:
    return runtime.to_host(path, ctx.cwd)


# ─── Komendy ────────────────────────────────────────────────────────────────

_SYSTEMCTL_ROUTINE = {"start", "restart", "reload", "try-restart", "reload-or-restart", "try-reload-or-restart",
                      "condrestart", "force-reload", "enable", "reset-failed", "daemon-reload", "stop", "disable"}
# Uslugi, bez ktorych traci sie dostep do serwera albo sam Pipe: zatrzymanie (a dla czesci i restart) pyta.
_LIFELINE_UNITS = {"docker", "containerd", "networking", "systemd-networkd", "networkmanager", "network",
                   "systemd-resolved", "dbus", "systemd-logind", "systemd-journald", "pipe", "cloud-init"}
_SSH_UNITS = {"ssh", "sshd", "ssh.socket"}
_FIREWALL_UNITS = {"ufw", "firewalld", "nftables", "iptables", "ip6tables", "netfilter-persistent", "fail2ban"}
_FILE_PROGRAMS = {"rm", "unlink", "truncate", "cp", "install", "ln", "mv", "tee", "touch", "mkdir", "sed",
                  "chmod", "chown", "chgrp"}
_ACCESS_PROGRAMS = {"ufw", "iptables", "ip6tables", "iptables-restore", "ip6tables-restore", "nft", "firewall-cmd",
                    "useradd", "userdel", "usermod", "adduser", "deluser", "groupadd", "groupdel", "groupmod", "addgroup",
                    "delgroup", "gpasswd", "passwd", "chpasswd", "chage", "visudo", "setfacl", "ssh-keygen", "ssh-copy-id",
                    "setcap", "fail2ban-client"}
_EGRESS_PROGRAMS = {"curl", "wget", "scp", "rsync", "sftp", "ftp", "nc", "ncat", "netcat", "socat", "telnet", "ssh"}
_CODE_PROGRAMS = {"pip", "pip3", "pipx", "npm", "npx", "yarn", "pnpm", "gem", "cargo", "go", "composer", "bun",
                  "bash", "sh", "zsh", "dash", "python", "python3", "perl", "ruby", "node", "php", "make", "source", ".",
                  "eval", "exec", "xargs", "env", "nohup", "timeout", "nice", "watch", "script", "at", "batch",
                  "systemd-run"}
_REBOOT_PROGRAMS = {"reboot", "shutdown", "poweroff", "halt", "init", "telinit"}
_PROCESS_PROGRAMS = {"kill", "pkill", "killall", "skill"}
_PACKAGE_ROUTINE = {
    "apt": {"install", "reinstall", "update", "upgrade", "dist-upgrade", "full-upgrade", "clean", "autoclean"},
    "apt-get": {"install", "reinstall", "update", "upgrade", "dist-upgrade", "clean", "autoclean", "check"},
    "dnf": {"install", "reinstall", "update", "upgrade", "makecache", "clean", "check-update"},
    "yum": {"install", "reinstall", "update", "upgrade", "makecache", "clean", "check-update"},
    "apk": {"add", "update", "upgrade", "fix", "cache"},
}
_PACKAGE_DELETE = {"remove", "purge", "autoremove", "erase", "del", "autopurge"}
# Zmienne przed komenda, ktore nie zmieniaja tego, co sie wykona (LD_PRELOAD, PATH — zmieniaja: pytanie)
_HARMLESS_ENV = {"DEBIAN_FRONTEND", "NEEDRESTART_MODE", "NEEDRESTART_SUSPEND", "APT_LISTCHANGES_FRONTEND", "LC_ALL",
                 "LANG", "LANGUAGE", "TERM", "PAGER", "SYSTEMD_PAGER", "TZ"}
# apt -o: tylko odpowiedzi na pytania o pliki konfiguracji i czekanie na blokade (Pre-Invoke itp. uruchamia kod)
_APT_OPTION = re.compile(r"^(Dpkg::Options::=?[\"']?--force-conf(def|old|new|miss)[\"']?|DPkg::Lock::Timeout=\d+"
                         r"|APT::Get::Assume-Yes=(true|1))$", re.IGNORECASE)


def command_reason(command: str, ctx: Context | None = None) -> str:
    """Dlaczego komenda powloki czeka na TAK ('' — Pipe wykona ja sam przez bezpiecznik)."""
    ctx = ctx or Context()
    segments, _writes, dynamic = security.split_command(command)
    if dynamic:
        return _why("dynamic")
    if not segments:
        return _why("unknown")
    programs = [_program(segment) for segment in segments]
    for segment in segments:
        why = _segment_reason(segment, ctx, command, programs)
        if why:
            return why
    return ""


def _program(segment: str) -> str:
    tokens = _strip_wrappers(security._tokens(segment) or [])
    return os.path.basename(tokens[0]) if tokens else ""


def _web_conf_inline(command: str, programs: list[str]) -> str:
    """
    Zapis do konfiguracji serwera WWW przez `>` albo `tee`: tresc musi byc w samej komendzie (echo/printf), bez
    dyrektyw wystawiajacych katalogi. `cat plik > /etc/nginx/...` przenioslby tresc, ktorej regula nie widzi.
    """
    if _EXPOSE.search(command) or any(p not in ("echo", "printf", "tee") for p in programs):
        return _why("expose")
    return ""


def _redirect_targets(segment: str) -> list[str]:
    from backend.core.safety import _redirect_targets as targets
    return targets(segment)


def _strip_wrappers(tokens: list[str]) -> list[str] | None:
    """`sudo [-n|-E|-H|-u user] cmd` -> cmd. Inne opcje sudo (np. -s, -i) — None (nie rozpoznaje)."""
    while tokens and os.path.basename(tokens[0]) in ("sudo", "doas"):
        i = 1
        while i < len(tokens) and tokens[i].startswith("-"):
            if tokens[i] in ("-n", "-E", "-H", "--non-interactive", "--preserve-env"):
                i += 1
            elif tokens[i] in ("-u", "--user") and i + 1 < len(tokens):
                i += 2
            else:
                return None
        tokens = tokens[i:]
    return tokens or None


def _segment_reason(segment: str, ctx: Context, command: str = "", programs: list[str] | None = None) -> str:
    tokens = security._tokens(segment)
    if not tokens:
        return _why("unknown")
    if security.is_sensitive(segment):
        return _why("secret")
    redirects = _redirect_targets(segment)
    if redirects:
        if ctx.remote:
            return _why("remote")
        for target in redirects:
            why = path_reason(_host(target, ctx), ctx)
            if not why and _WEB_CONF.search(_host(target, ctx)):
                why = _web_conf_inline(command or segment, programs or [_program(segment)])
            if why:
                return why
            if _too_big(_host(target, ctx)) and ">>" not in segment:
                return _why("delete")
    tokens = [t for t in tokens if not security._HARMLESS_REDIRECT.match(t) and t not in ("2>&1", "1>&2")]
    tokens = [t for t in tokens if not re.match(r"^\d?>>?", t)]          # `> plik` sprawdzone wyzej
    if redirects:
        tokens = [t for t in tokens if t.strip("'\"") not in redirects]
    if not tokens:
        return _why("unknown")
    if security.classify_segment(" ".join(shlex.quote(t) for t in tokens)) == "safe":
        return ""
    tokens = _strip_wrappers(tokens)
    while tokens and re.match(r"^[A-Za-z_]\w*=", tokens[0]) and tokens[0].split("=", 1)[0] in _HARMLESS_ENV:
        tokens = tokens[1:]
    if not tokens or re.match(r"^\w+=", tokens[0]):
        return _why("unknown")
    program = os.path.basename(tokens[0])
    if program == "curl":
        return "" if security._check_curl(tokens, also_local=ctx.own_hosts) == "safe" else _why("egress")
    if program in _EGRESS_PROGRAMS:
        return _why("egress")
    if program in _ACCESS_PROGRAMS:
        return _why("access")
    if program in _REBOOT_PROGRAMS:
        return _why("reboot")
    if program in _PROCESS_PROGRAMS:
        return _why("process")
    if program in _CODE_PROGRAMS:
        return _why("code")
    if program == "crontab":
        return _why("startup")
    if program in ("systemctl", "service"):
        return _systemctl(tokens)
    if program in ("docker", "docker-compose"):
        return _docker(tokens, segment, ctx)
    if program in _PACKAGE_ROUTINE or program == "aptitude":
        return _packages(program, tokens)
    if program == "dpkg":
        return "" if tokens[1:] in (["--configure", "-a"], ["--configure", "--pending"]) else _why("system")
    if program == "git":
        return _git(tokens)
    if program == "kubectl":
        return _kubectl(tokens)
    if program == "helm":
        return "" if tokens[1:2] == ["rollback"] else _why("code")
    if program == "nginx":
        signal = tokens[tokens.index("-s") + 1] if "-s" in tokens[:-1] else ""
        return "" if signal in ("reload", "reopen") else _why("outage")
    if program in ("apachectl", "apache2ctl"):
        return "" if tokens[1:2] in (["graceful"], ["restart"], ["configtest"], ["-k"]) and "stop" not in tokens \
            and "graceful-stop" not in tokens else _why("outage")
    if program == "caddy":
        return "" if tokens[1:2] == ["reload"] else _why("unknown")
    if program == "certbot":
        return "" if tokens[1:2] == ["renew"] else _why("unknown")
    if program == "journalctl":
        return ""                                   # --vacuum / --rotate — sprzatanie starych logow
    if program in _FILE_PROGRAMS:
        return _files(program, tokens, segment, ctx, command or segment, programs or [program])
    return _why("unknown")


def _systemctl(tokens: list[str]) -> str:
    program = os.path.basename(tokens[0])
    positional = [t for t in tokens[1:] if not t.startswith("-")]
    if program == "service":
        if len(positional) < 2:
            return _why("unknown")
        units, action = [positional[0]], positional[1]
    else:
        if not positional:
            return _why("unknown")
        action, units = positional[0], positional[1:]
    if action not in _SYSTEMCTL_ROUTINE or any(t in ("--global", "--runtime", "--force", "-f") for t in tokens):
        return _why("system")
    for unit in units:
        if any(c in unit for c in "*?[") or "@" in unit and unit.split("@", 1)[0].lower() in ("getty", "serial-getty"):
            return _why("outage")
        base = unit.lower().removesuffix(".service")
        if base in _LIFELINE_UNITS and action != "start":
            return _why("outage")
        if base in _SSH_UNITS | {"ssh.socket"} and action in ("stop", "disable"):
            return _why("outage")
        if base in _FIREWALL_UNITS and action not in ("start", "enable", "reload"):
            return _why("access")
    return ""


def _own(name: str, ctx: Context) -> bool:
    name = name.lower()
    if name in {n.lower() for n in ctx.own_names} or name.startswith("pipe-web"):
        return True
    project = ctx.own_project.lower()
    return bool(project) and (name.startswith(f"{project}-") or name.startswith(f"{project}_"))


def _docker(tokens: list[str], segment: str, ctx: Context) -> str:
    program = os.path.basename(tokens[0])
    compose = program == "docker-compose" or tokens[1:2] == ["compose"]
    if compose:
        args = tokens[1:] if program == "docker-compose" else tokens[2:]
        sub, flags, rest = "", [], []
        i = 0
        while i < len(args):
            token = args[i]
            if not sub and token in ("-f", "--file", "-p", "--project-name", "--project-directory", "--env-file",
                                     "--profile", "--ansi", "--progress", "--parallel"):
                flags += args[i:i + 2]
                i += 2
                continue
            if not sub and token.startswith("-"):
                flags.append(token)
            elif not sub:
                sub = token
            else:
                rest.append(token)
            i += 1
        project = flags[flags.index("-p") + 1] if "-p" in flags[:-1] else \
            flags[flags.index("--project-name") + 1] if "--project-name" in flags[:-1] else ""
        if ctx.own_project and project.lower() == ctx.own_project.lower():
            return _why("pipe")
        if sub in ("up", "restart", "start", "stop", "pull", "build", "create", "kill", "pause", "unpause"):
            if any(t in ("-V", "--renew-anon-volumes") for t in rest):
                return _why("delete")
            return ""
        if sub == "down":
            return _why("delete") if any(t in ("-v", "--volumes", "--rmi") or t.startswith("--rmi=") for t in rest) else ""
        return _why("code") if sub in ("run", "exec", "cp") else _why("delete") if sub == "rm" else _why("unknown")

    sub = tokens[1] if len(tokens) > 1 else ""
    if sub in ("container", "image") and len(tokens) > 2:          # `docker container restart` = `docker restart`
        sub = f"{sub} {tokens[2]}"
        names_from = 3
    else:
        names_from = 2
    if sub in ("restart", "start", "stop", "kill", "pause", "unpause", "container restart", "container start",
               "container stop", "container kill"):
        names = _names(tokens[names_from:], ("-t", "--time", "-s", "--signal"))
        if not names:
            return _why("unknown")
        return _why("pipe") if any(_own(n, ctx) for n in names) else ""
    if sub in ("pull", "image pull"):
        return ""
    if sub in ("image prune", "builder prune", "buildx prune"):
        return _why("delete") if sub == "image prune" and any(t in ("-a", "--all") or (t.startswith("-") and not
                                                              t.startswith("--") and "a" in t) for t in tokens[3:]) else ""
    if sub in ("run", "exec", "container run", "container exec", "create", "container create", "cp", "commit",
               "build", "load", "import"):
        return _why("container") if _RISKY_CONTAINER.search(segment) else _why("code")
    if sub in ("volume", "network"):
        return _why("delete") if tokens[2:3] and tokens[2] in ("rm", "prune") else _why("unknown")
    if sub in ("rm", "rmi", "container rm", "image rm", "system", "container prune"):
        return _why("delete")
    return _why("unknown")


def _names(tokens: list[str], value_flags: tuple[str, ...]) -> list[str]:
    """Argumenty pozycyjne (bez flag i wartosci flag z `value_flags`)."""
    out, skip = [], False
    for token in tokens:
        if skip:
            skip = False
        elif token in value_flags:
            skip = True
        elif not token.startswith("-"):
            out.append(token)
    return out


def _packages(program: str, tokens: list[str]) -> str:
    positional = _names(tokens[1:], ("-o", "--option", "-c", "--config-file", "-t", "--target-release"))
    action = positional[0] if positional else ""
    if action in _PACKAGE_DELETE:
        return _why("delete")
    if action not in _PACKAGE_ROUTINE.get(program, _PACKAGE_ROUTINE["apt"]):
        return _why("system")
    # -o Dpkg::Pre-Invoke=..., -c plik, pakiet z pliku albo bez podpisu — to uruchamianie obcego kodu
    args = tokens[1:]
    for i, token in enumerate(args):
        if token in ("-o", "--option"):
            value = args[i + 1] if i + 1 < len(args) else ""
        elif token.startswith("--option="):
            value = token.split("=", 1)[1]
        elif token.startswith("-o"):
            value = token[2:]
        else:
            value = None
        if value is not None and not _APT_OPTION.match(value):
            return _why("code")
        if token in ("-c", "--config-file", "--allow-unauthenticated", "--allow-untrusted",
                     "--allow-insecure-repositories", "--nogpgcheck") or token.startswith("--config-file="):
            return _why("code")
    options = {args[i + 1] for i, t in enumerate(args[:-1]) if t in ("-o", "--option")}
    names = [p for p in positional[1:] if p not in options]
    if any(p.startswith(("/", "./", "../", "~")) or p.endswith((".deb", ".rpm", ".apk")) for p in names):
        return _why("code")
    return ""


_GIT_ROUTINE = {"pull", "fetch", "checkout", "switch", "stash", "merge", "add", "commit", "tag", "restore", "status",
                "revert", "cherry-pick"}


def _git(tokens: list[str]) -> str:
    args = tokens[1:]
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] in ("-C", "--git-dir", "--work-tree"):
            i += 2
            continue
        return _why("code")                          # -c core.pager=... i inne opcje globalne
    sub, rest = (args[i], args[i + 1:]) if i < len(args) else ("", [])
    if sub == "push":
        return _why("egress")
    if sub in ("reset", "clean", "gc", "prune", "rm", "branch", "rebase", "worktree"):
        if sub == "reset" and not any(t in ("--hard", "--keep", "--merge") for t in rest):
            return ""
        return _why("delete")
    if sub in ("remote", "config", "submodule", "lfs", "hook", "filter-branch", "replace", "update-ref"):
        return _why("code")
    if sub not in _GIT_ROUTINE:
        return _why("unknown")
    if sub in ("checkout", "restore") and ("--" in rest or "." in rest or any(t in ("-f", "--force", "-p") for t in rest)
                                           or (sub == "restore" and "--staged" not in rest)):
        return _why("delete")                        # odrzuca niezapisane zmiany w plikach
    if sub == "stash" and rest[:1] and rest[0] in ("drop", "clear"):
        return _why("delete")
    if sub in ("pull", "fetch", "merge") and any(t.startswith(("--upload-pack", "--exec", "-c")) for t in rest):
        return _why("code")
    names = [t for t in rest if not t.startswith("-")]
    if sub in ("pull", "fetch") and names and not re.fullmatch(r"[A-Za-z0-9._-]+", names[0]):
        return _why("code")                          # adres zamiast nazwy remote — cudzy kod w aplikacji
    return ""


def _kubectl(tokens: list[str]) -> str:
    positional = _names(tokens[1:], ("-n", "--namespace", "--context", "--kubeconfig", "--cluster", "--user", "-l",
                                     "--selector", "-c", "--container", "-o", "--output", "--replicas", "--timeout"))
    sub = positional[0] if positional else ""
    if sub == "scale" or (sub == "rollout" and positional[1:2] and positional[1] in ("restart", "undo", "resume",
                                                                                      "pause", "status")):
        return ""
    if sub in ("delete", "drain", "cordon", "uncordon", "taint"):
        return _why("delete")
    return _why("code")                              # apply/create/patch/exec — w tym RBAC i dowolne obiekty


_SETUID = re.compile(r"^[0-7]?[2-7][0-7]{3}$")


def _chmod_reason(mode: str) -> str:
    if _SETUID.match(mode) or (not mode.isdigit() and "s" in mode):
        return _why("access")
    if mode.isdigit() and mode[-1] in "2367":
        return _why("access")                        # zapis dla wszystkich
    if not mode.isdigit() and re.search(r"(^|,)([ao]+|)[+=][rwxXt]*w", mode) and not re.search(r"(^|,)[ug]+[+=]", mode):
        return _why("access")
    return ""


def _files(program: str, tokens: list[str], segment: str, ctx: Context, command: str = "",
           programs: list[str] | None = None) -> str:
    from backend.core.safety import _files_for_program, _positional

    args = tokens[1:]
    if ctx.remote:
        return _why("remote")
    if program == "rm" and any(a.startswith("-") and not a.startswith("--") and "r" in a.lower() or a == "--recursive"
                               for a in args):
        return _why("delete")
    if program == "sed":
        if not any(a.startswith(("-i", "--in-place")) for a in args):
            return _why("unknown")
        script = " ".join(a for a in args if not a.startswith("-"))
        if re.search(r"(^|[;{}\n/\d$])\s*[eEwWrR](\s|$)", script):
            return _why("code")                      # e = wykonanie, w/r = zapis/odczyt innego pliku
    if program in ("chmod", "chown", "chgrp"):
        positional = _positional(args, ("--reference",))
        if program == "chmod" and positional:
            why = _chmod_reason(positional[0])
            if why:
                return why
    if program == "mkdir":
        files = [_host(p, ctx) for p in _positional(args, ("-m", "--mode"))]
    else:
        files, _notes = _files_for_program(tokens, ctx.cwd)
    if any(c in segment for c in "*?") and program in ("rm", "mv", "cp", "chmod", "chown", "chgrp", "truncate"):
        return _why("delete")                        # glob — bezpiecznik nie wie, ktore pliki skopiowac
    if not files:
        return _why("unknown")
    for path in files:
        why = path_reason(path, ctx)
        if why:
            return why
    overwritten = files[len(files) // 2:] if program == "mv" else files      # mv: zrodla zostaja pod nowa nazwa
    if program in ("rm", "unlink", "truncate", "mv", "cp", "install", "tee", "sed")             and any(_too_big(p) for p in overwritten):
        return _why("delete")
    if any(_WEB_CONF.search(p) for p in files):
        return _web_conf_files(program, args, files, ctx, command, programs)
    return ""


def _web_conf_files(program: str, args: list[str], files: list[str], ctx: Context, command: str,
                    programs: list[str]) -> str:
    """
    Zmiana konfiguracji serwera WWW komenda: `sed -i` bez dyrektyw wystawiajacych katalogi, `cp`/`mv`/`ln` tylko
    w obrebie tego samego katalogu serwera (np. sites-available -> sites-enabled, kopia .bak), `tee` z trescia
    w komendzie. Plik przygotowany gdzie indziej (np. w /tmp) ma tresc, ktorej regula nie widzi.
    """
    from backend.core.safety import _positional

    if program == "sed":
        return _why("expose") if _EXPOSE.search(" ".join(a for a in args if not a.startswith("-"))) else ""
    if program == "tee":
        return _web_conf_inline(command, programs)
    if program in ("cp", "mv", "install", "ln"):
        positional = _positional(args, ("-t", "--target-directory", "-m", "--mode", "-o", "--owner", "-g", "--group",
                                        "-S", "--suffix"))
        roots = {_conf_root(p) for p in files if _WEB_CONF.search(p)}
        sources = [_host(p, ctx) for p in positional[:-1]]
        if not sources or any(_conf_root(src) not in roots for src in sources):
            return _why("expose")
    return ""


def _conf_root(host_path: str) -> str:
    """Katalog serwera WWW sciezki: /etc/nginx/sites-enabled/x -> /etc/nginx."""
    parts = os.path.normpath(host_path).split("/")
    return "/".join(parts[:3]) if len(parts) > 3 and parts[1] == "etc" else os.path.dirname(host_path)


# ─── Potwierdzenie ──────────────────────────────────────────────────────────

def reason(pending, ctx: Context | None = None) -> str:
    """
    Dlaczego operacja (session.ConfirmationRequest) czeka na TAK ('' — Pipe wykona ja sam).

    Operacje narzedzi (`action`, `stream`) sa rutyna tylko z `auto=True` od handlera (latki, cofniecie zmiany,
    zadanie jednorazowe, aktualizacja Pipe, uruchomienie projektu). `auto=False` — zawsze pyta (np. model sam
    poprosil o potwierdzenie). Komendy i zapisy plikow ocenia sie tu.
    """
    ctx = ctx or Context()
    if pending.auto is False:
        return _why("tool")
    if pending.stream is not None or pending.action is not None:
        return "" if pending.auto else _why("tool")
    if pending.tool_name == "write_file":
        host = runtime.to_host(pending.file_path or "")
        return path_reason(host, ctx, pending.file_content) or (_why("delete") if _too_big(host) else "")
    return command_reason(pending.inner or pending.command, ctx)
