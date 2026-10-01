"""
Security — klasyfikacja komend na trzy poziomy bezpieczenstwa.

  forbidden  odmow bezwzglednie (nie jest zwracane modelowi jako blad do ponowienia)
  confirm    popros uzytkownika o potwierdzenie
  safe       wykonaj od razu

Zasada: fail-closed. `safe` jest tylko komenda, ktora w CALOSCI rozpoznajemy
jako odczyt. Wszystko inne — nieznany program, przekierowanie do pliku,
podstawienie `$(...)`, flaga zmieniajaca stan (`find -delete`, `curl -o`,
`git branch -D`) — wymaga potwierdzenia.

Kolejnosc:
  1. wzorce FORBIDDEN na calym napisie (zeby zlapac `curl ... | bash`),
  2. podzial na segmenty po `;`, `|`, `||`, `&&`, `&` i nowych liniach —
     z uwzglednieniem cudzyslowow, wiec `grep 'a|b'` to jeden segment,
  3. kazdy segment: forbidden -> wrazliwe pliki -> wzorce confirm ->
     rozpoznany odczyt (tokenowo, nie prefiksem napisu: `ss` to nie `ssh`),
  4. przekierowanie zapisu albo dynamiczna konstrukcja powloki -> confirm.

Dopasowanie jest tokenowe (shlex), bez wielkosci liter.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Literal

from backend.core import runtime
from backend.core.i18n import tr

Classification = Literal["safe", "confirm", "forbidden"]

# ─── Poziom 1: SAFE — rozpoznane odczyty ────────────────────────────────────
# Kazdy wpis to poczatek komendy liczony w tokenach: "git log" pasuje do
# `git log -5`, ale "ss" nie pasuje do `ssh`. Wpisy z analizatorem argumentow
# (git, find, curl, journalctl, kubectl, helm, docker, systemctl, sed, ip)
# sa sprawdzane dodatkowo w _check_arguments().
SAFE_PREFIXES: list[str] = [
    # pliki i tekst
    "cat", "head", "tail", "less -f", "grep", "egrep", "fgrep", "zgrep", "zcat", "wc", "sort", "uniq",
    "cut", "column", "jq", "stat", "file", "ls", "tree", "find", "du", "df", "realpath", "readlink",
    "basename", "dirname", "echo", "printf", "true", "which", "whereis", "md5sum", "sha256sum", "diff",
    # system
    "free", "uptime", "ps", "pgrep", "top", "hostname", "whoami", "id", "pwd", "date", "uname", "nproc",
    "lsblk", "lscpu", "lsof", "findmnt", "vmstat", "iostat", "mpstat", "sensors", "who", "w", "last",
    "timedatectl status", "hostnamectl status", "dpkg -l", "dpkg -L", "dpkg -s", "rpm -q", "apt list",
    "apt-cache policy", "apt-cache show", "apt-cache search", "snap list", "crontab -l",
    "systemctl status", "systemctl is-active", "systemctl is-enabled", "systemctl is-failed",
    "systemctl list-units", "systemctl list-unit-files", "systemctl list-timers", "systemctl show",
    "systemctl cat", "systemctl --failed", "journalctl",
    # siec
    "netstat", "ss", "ip a", "ip addr", "ip -br", "ip r", "ip route", "ip link show", "ip -4", "ip -6",
    "nslookup", "dig", "host", "ping", "traceroute", "tracepath", "getent", "curl", "openssl x509",
    "openssl s_client", "ufw status", "iptables -l", "iptables -s", "iptables -nl", "iptables -nvl",
    "nft list", "certbot certificates", "nginx -t", "nginx -T", "caddy validate", "caddy version",
    # docker
    "docker ps", "docker logs", "docker stats", "docker inspect", "docker images", "docker image ls",
    "docker top", "docker version", "docker info", "docker network ls", "docker network inspect",
    "docker volume ls", "docker volume inspect", "docker container ls", "docker system df",
    "docker compose ps", "docker compose logs", "docker compose config", "docker compose ls",
    "docker compose images", "docker-compose ps", "docker-compose logs", "docker-compose config",
    # git (argumenty sprawdza _check_git)
    "git status", "git log", "git diff", "git branch", "git remote", "git show", "git tag",
    "git rev-parse", "git ls-files", "git blame", "git describe", "git shortlog", "git reflog",
    "git config --get", "git config -l", "git config --list", "git stash list", "git fetch --dry-run",
    # kubernetes / helm
    "kubectl get", "kubectl describe", "kubectl logs", "kubectl top", "kubectl explain",
    "kubectl api-resources", "kubectl api-versions", "kubectl version", "kubectl cluster-info",
    "kubectl config view", "kubectl config get-contexts", "kubectl config current-context",
    "kubectl auth can-i", "kubectl events", "kubectl rollout status", "kubectl rollout history",
    "helm list", "helm ls", "helm status", "helm get", "helm history", "helm version", "helm show",
    "helm search", "helm repo list",
]

# ─── Poziom 2: CONFIRM ──────────────────────────────────────────────────────
# Nieznane komendy i tak sa `confirm`; te wzorce przesadzaja sprawe takze
# wtedy, gdy segment zaczyna sie od rozpoznanego odczytu.
CONFIRM_PATTERNS: list[str] = [
    r"\bchmod\s", r"\bchown\s", r"\bufw\s+(?!status)", r"\biptables\s+-[^lsn]", r"\bcrontab\s+(?!-l)",
    r"\bpasswd\b", r"\breboot\b", r"\bshutdown\b", r"\bpoweroff\b", r"\bhalt\b",
    r"\bapt(-get)?\s+(install|remove|purge|autoremove|upgrade|dist-upgrade|full-upgrade)",
    r"\bmv\s", r"\bcp\s", r"\brm\s", r"\bkill\s", r"\bpkill\s", r"\bkillall\s", r"\bservice\s",
    r"\bsystemctl\s+(start|stop|restart|reload|enable|disable|mask|unmask|daemon-reload|kill|edit)\b",
    r"\bdocker\s+(stop|start|restart|rm|rmi|kill|run|exec|system\s+prune|volume\s+rm|network\s+rm|compose\s+(up|down|rm|stop|restart|pull))\b",
    r"\bdocker-compose\s+(up|down|rm|stop|restart|pull)\b",
    r"\bpip3?\s+install\b",
    r"\bgit\s+(push|commit|checkout|switch|merge|rebase|reset|clean|pull|stash\s+(pop|drop|clear)|restore|rm|mv)\b",
    r"\bkubectl\s+(apply|create|delete|edit|patch|replace|scale|set|label|annotate|drain|cordon|uncordon|taint|exec|cp|port-forward|rollout\s+(restart|undo|pause|resume))\b",
    r"\bhelm\s+(install|upgrade|uninstall|delete|rollback)\b",
]

# Odczyt tych sciezek wysylalby sekrety do providera LLM — zawsze z potwierdzeniem.
SENSITIVE_PATTERNS: list[str] = [
    r"/etc/g?shadow\b",
    r"\.ssh/(?!known_hosts|config\b|authorized_keys)",
    r"\bid_(rsa|dsa|ecdsa|ed25519)\b",
    r"ssh_host_\w+_key\b(?!\.pub)",
    r"\.(pem|key|p12|pfx)\b",
    r"(^|[/\s'\"=])\.env(\.[\w.-]+)?\b",
    # /proc/<pid>/environ|root|cwd|mem — przy `pid: host` to sekrety i katalogi domowe procesow hosta,
    # obejscie read-only /hostfs. Dotyczy tez /hostproc.
    r"/proc/[^/\s]+/(environ|root|cwd|mem|maps|task)\b",
    r"/hostproc/[^/\s]+/(environ|root|cwd|mem|maps|task)\b",
    r"\bkubectl\s+get\s+secrets?\b.*-o\s*(yaml|json|jsonpath|go-template)",
    r"\bkubectl\s+get\s+secrets?\b.*--output",
    # Globy w katalogach z sekretami — omijaja wzorce wyzej (np. cat /etc/s*adow)
    r"(^|\s)(/hostfs)?/etc/[^\s]*[*?\[][^\s]*",
    r"(^|\s)(/hostfs)?/(root|home/[^/\s]+)/\.[^\s]*[*?\[]",
]

# ─── Poziom 3: FORBIDDEN ────────────────────────────────────────────────────
FORBIDDEN_PATTERNS: list[str] = [
    r"\brm\s+(-[a-z]*\s+)*-[a-z]*r[a-z]*\s+(-[a-z]*\s+)*[\"']?(/|/\*|~|~/|\$home|/hostfs/?)[\"']?(?=\s|$|[);&|'\"])",
    r"\brm\s+--no-preserve-root",
    r"\bdd\s+if=",
    r"\bmkfs(\.|\s)",
    r">\s*/etc/passwd",
    r">\s*/etc/shadow",
    r":\(\)\s*\{",                 # fork bomb
    r"\b(curl|wget)\b.+\|\s*(sudo\s+)?(ba|z|da)?sh\b",
    r"\bbase64\b.*\|\s*(ba|z|da)?sh\b",
    r"\bpython[0-9.]*\s+-c.+exec",
    r">\s*/dev/(sd|vd|nvme|xvd|hd)",  # nadpisanie dysku
    r"\bshred\s",
    r">\s*/boot/",
    r"\bwipefs\b",
    r"\bkubectl\s+delete\s+(ns|namespaces?)\s+(kube-system|default)\b",
    r"\bkubectl\s+delete\s+.*--all\s+--all-namespaces",
]

_compiled_forbidden = [re.compile(p, re.IGNORECASE) for p in FORBIDDEN_PATTERNS]
_compiled_confirm = [re.compile(p, re.IGNORECASE) for p in CONFIRM_PATTERNS]
_compiled_sensitive = [re.compile(p, re.IGNORECASE) for p in SENSITIVE_PATTERNS]
_safe_tokens: list[tuple[str, ...]] = sorted(
    (tuple(p.lower().split()) for p in SAFE_PREFIXES), key=len, reverse=True
)

# Przekierowania, ktore nie zapisuja plikow.
_HARMLESS_REDIRECT = re.compile(r"^(\d?>&\d|\d?>\s*/dev/null|&>\s*/dev/null|\d?>>\s*/dev/null)")


# ─── Leksyka powloki ────────────────────────────────────────────────────────

def split_command(cmd: str) -> tuple[list[str], bool, bool]:
    """
    Dzieli komende na segmenty po operatorach sterujacych poza cudzyslowami.

    Returns:
        (segmenty, jest_zapis_do_pliku, jest_dynamiczna_konstrukcja)
        Zapis to `>`/`>>` do czegos innego niz /dev/null albo deskryptor.
        Dynamiczna konstrukcja to `$(`, `${`, `$'...'`, backtick, `<(`, `>(` —
        ich wynik (np. `${X:-shadow}`, `$'sh\\x61dow'`) nie jest znany przed wykonaniem.
    """
    segments: list[str] = []
    buf: list[str] = []
    writes = dynamic = False
    quote: str | None = None
    i = 0
    n = len(cmd)

    def flush() -> None:
        segment = "".join(buf).strip()
        if segment:
            segments.append(segment)
        buf.clear()

    while i < n:
        ch = cmd[i]
        if quote == "'":
            buf.append(ch)
            if ch == "'":
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(cmd[i:i + 2])
            i += 2
            continue
        if quote == '"':
            if ch == '"':
                quote = None
            elif ch == "`" or cmd.startswith(("$(", "${"), i):
                dynamic = True
            buf.append(ch)
            i += 1
            continue
        # poza cudzyslowami
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "`" or cmd.startswith(("$(", "${", "$'", '$"', "<(", ">("), i):
            dynamic = True
            buf.append(ch)
            i += 1
            continue
        if ch in ";\n" or cmd.startswith("&&", i) or cmd.startswith("||", i):
            flush()
            i += 2 if cmd.startswith(("&&", "||"), i) else 1
            continue
        if ch == "|":
            flush()
            i += 2 if cmd.startswith("|&", i) else 1
            continue
        if ch == "&":
            # `2>&1` i `&>/dev/null` to przekierowania, nie tlo
            prev = cmd[i - 1] if i else ""
            if prev in "<>" or cmd.startswith("&>", i):
                buf.append(ch)
                i += 1
                continue
            flush()
            i += 1
            continue
        if ch == ">":
            # `N>`, `&>` — cofnij sie do poczatku operatora
            start = i - 1 if i and (cmd[i - 1].isdigit() or cmd[i - 1] == "&") else i
            if not (i and cmd[i - 1] == ">") and not _HARMLESS_REDIRECT.match(cmd[start:]):
                writes = True
        buf.append(ch)
        i += 1

    flush()
    if quote:
        dynamic = True  # niezamkniety cudzyslow — nie ufamy podzialowi
    return segments, writes, dynamic


def _tokens(segment: str) -> list[str] | None:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return None


# ─── Analiza argumentow rozpoznanych odczytow ───────────────────────────────

def _check_git(tokens: list[str]) -> Classification:
    """`git [-C dir] <sub> ...` — opcje globalne nie moga ukryc zapisu."""
    args = tokens[1:]
    rest: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-C", "--git-dir", "--work-tree", "--namespace"):
            i += 2
            continue
        if arg.startswith(("--git-dir=", "--work-tree=", "--namespace=")):
            i += 1
            continue
        if arg in ("-c", "--exec-path") or arg.startswith(("-c", "--config-env", "--exec-path")):
            return "confirm"  # -c core.pager=... / core.sshCommand=... wykonuje dowolny program
        if arg in ("--no-pager", "--paginate", "-p", "-P", "--no-optional-locks", "--bare"):
            i += 1
            continue
        rest = args[i:]
        break
    if not rest:
        return "confirm"
    sub, sub_args = rest[0].lower(), rest[1:]
    # Flagi, ktore w dowolnej podkomendzie uruchamiaja wskazany program albo pisza do pliku.
    # Wartosc moze byc dolaczona (`--upload-pack=id`) albo w nastepnym tokenie (`-O id`).
    for arg in sub_args:
        name = arg.split("=", 1)[0].lower()
        if name in ("--upload-pack", "--receive-pack", "--exec", "-o", "--open-files-in-pager", "--output",
                    "--to-command", "--ext-diff") or name.startswith("-o"):
            return "confirm"
    if sub == "branch":
        # Tylko listowanie: `git branch`, `-a`, `-r`, `-v`, `--list wzorzec`, `--contains X`...
        listing = {"-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose", "--list", "-l",
                   "--show-current", "--merged", "--no-merged", "--contains", "--no-contains"}
        if any(a.startswith("-") and a.split("=")[0] not in listing and not a.startswith(("--sort", "--format",
                                                                                           "--color", "--points-at"))
               for a in sub_args):
            return "confirm"
        if any(not a.startswith("-") for a in sub_args) and not ({"--list", "-l", "--contains", "--merged",
                                                                  "--no-merged", "--no-contains"} & set(sub_args)):
            return "confirm"  # `git branch nowa` tworzy galaz
    if sub == "tag":
        listing = {"-l", "--list", "-n"}
        if any(a.startswith("-") and a not in listing and not a.startswith(("-n", "--sort", "--contains",
                                                                              "--points-at", "--format"))
               for a in sub_args):
            return "confirm"
        if any(not a.startswith("-") for a in sub_args) and not ({"-l", "--list"} & set(sub_args)):
            return "confirm"  # `git tag v1` tworzy tag
    if sub == "remote" and sub_args and sub_args[0] not in ("-v", "--verbose", "show", "get-url"):
        return "confirm"
    if sub == "config" and not any(a in ("--get", "-l", "--list", "--get-all", "--get-regexp") for a in sub_args):
        return "confirm"
    if sub == "reflog" and sub_args and sub_args[0] in ("expire", "delete"):
        return "confirm"
    if sub == "stash" and (not sub_args or sub_args[0] not in ("list", "show")):
        return "confirm"
    safe_subs = {"status", "log", "diff", "branch", "remote", "show", "tag", "rev-parse", "ls-files", "blame",
                 "describe", "shortlog", "reflog", "config", "stash", "ls-remote", "rev-list", "cat-file",
                 "grep", "whatchanged", "count-objects", "for-each-ref", "name-rev", "check-ignore", "version"}
    if sub == "fetch" and "--dry-run" in sub_args:
        return "safe"
    if any(a.startswith("--output") or a == "-o" for a in sub_args) and sub in ("diff", "log", "show"):
        return "confirm"
    if sub == "diff" and "--ext-diff" in sub_args:
        return "confirm"
    return "safe" if sub in safe_subs else "confirm"


_CURL_WRITE_FLAGS = (
    "-o", "--output", "-O", "--remote-name", "--remote-name-all", "-T", "--upload-file",
    "-d", "--data", "--data-binary", "--data-raw", "--data-ascii", "--data-urlencode", "-F", "--form",
    "-K", "--config", "-c", "--cookie-jar", "--json", "-D", "--dump-header", "--trace", "--trace-ascii",
    "--libcurl", "--etag-save", "--hsts", "--alt-svc", "--create-dirs", "--output-dir", "--remote-header-name",
)
# Flagi curl, po ktorych nastepny token jest wartoscia, a nie URL-em.
_CURL_VALUE_FLAGS = frozenset({
    "-H", "--header", "-A", "--user-agent", "-e", "--referer", "-X", "--request", "-u", "--user",
    "-b", "--cookie", "-d", "--data", "--data-binary", "--data-raw", "--data-ascii", "--data-urlencode",
    "-F", "--form", "-o", "--output", "-T", "--upload-file", "-K", "--config", "--url", "--proxy", "-x",
    "--connect-to", "--resolve", "-w", "--write-out", "--data-out", "--preproxy", "--doh-url",
    "--socks4", "--socks4a", "--socks5", "--socks5-hostname",
})
# Flagi kierujace ruch gdzie indziej niz URL: host z wartosci musi byc lokalny.
_CURL_PROXY_FLAGS = frozenset({"-x", "--proxy", "--preproxy", "--doh-url", "--socks4", "--socks4a", "--socks5",
                               "--socks5-hostname"})
# --resolve HOST:PORT:ADRES i --connect-to H1:P1:H2:P2 podmieniaja adres docelowy — zawsze confirm.
_CURL_REROUTE_FLAGS = frozenset({"--resolve", "--connect-to"})


def _host_is_local(host: str) -> bool:
    """True dla localhost, adresow prywatnych i nazw bez kropki (lokalna siec)."""
    import ipaddress

    host = host.strip().strip("[]").lower()
    if not host:
        return False
    if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost"):
        return True
    # curl rozumie adres IPv4 jako jedna liczbe (`134744072`, `0x08080808`) — to nie nazwa lokalna
    if re.fullmatch(r"0x[0-9a-f]+|\d+", host):
        try:
            address = ipaddress.ip_address(int(host, 16 if host.startswith("0x") else 10))
        except ValueError:
            return False
        return address.is_loopback or address.is_private or address.is_link_local
    try:
        address = ipaddress.ip_address(host)
        return address.is_loopback or address.is_private or address.is_link_local
    except ValueError:
        # nazwa hosta: bez kropki = lokalna; .local = mDNS; host.docker.internal = host
        return "." not in host or host.endswith(".local") or host == "host.docker.internal"


def _url_host(token: str) -> str | None:
    """Host z argumentu-URL-a curl (`https://h/p`, `h:443/p`, `h/p`, `h`)."""
    import re as _re
    value = _re.sub(r"^[a-z][a-z0-9+.-]*://", "", token, flags=_re.IGNORECASE)
    value = value.split("/", 1)[0].split("?", 1)[0]
    if value.startswith("["):                       # IPv6 w nawiasach
        return value[: value.find("]") + 1]
    if "@" in value:                                # user:pass@host
        value = value.rsplit("@", 1)[1]
    return value.rsplit(":", 1)[0] if ":" in value and not value.count(":") > 1 else value


def _check_curl(tokens: list[str]) -> Classification:
    """curl: pisanie do pliku, metoda inna niz GET albo host zewnetrzny -> confirm (eksfiltracja)."""
    method = "GET"
    skip = False
    hosts: list[str] = []
    for i, token in enumerate(tokens[1:], start=1):
        low = token.lower()
        name, _, inline = low.partition("=")
        if skip:
            skip = False
            continue
        if token in _CURL_WRITE_FLAGS or (inline and name in _CURL_WRITE_FLAGS):
            return "confirm"
        if not token.startswith("--") and token.startswith("-") and len(token) > 2 \
                and any(c in token[1:] for c in "oOTdFKcD"):
            return "confirm"                        # sklejone krotkie flagi, np. -sSo plik
        flag, has_value, value = token.partition("=")    # flagi curl rozrozniaja wielkosc liter
        if flag in _CURL_REROUTE_FLAGS:
            return "confirm"
        if flag in _CURL_PROXY_FLAGS or (token.startswith("-x") and len(token) > 2):
            if flag in _CURL_PROXY_FLAGS:
                if not has_value:
                    value = tokens[i + 1] if i + 1 < len(tokens) else ""
                    skip = True
            else:
                value = token[2:]                   # -xhost:port
            proxy = _url_host(value) if value else None
            if not proxy or not _host_is_local(proxy):
                return "confirm"                    # ruch przez obcy proxy = mozliwa eksfiltracja
            continue
        if flag in ("-X", "--request") and has_value:
            method = value
        elif token in ("-X", "--request"):
            method = tokens[i + 1] if i + 1 < len(tokens) else ""
            skip = True
        elif token.startswith("-X") and len(token) > 2:
            method = token[2:]
        elif name == "--url" and inline:
            host = _url_host(inline)
            if host:
                hosts.append(host)
        elif token in _CURL_VALUE_FLAGS:
            skip = True
        elif not token.startswith("-"):
            host = _url_host(token)
            if host:
                hosts.append(host)
    if method.upper() not in ("GET", "HEAD", "OPTIONS"):
        return "confirm"
    if any(not _host_is_local(host) for host in hosts):
        return "confirm"                            # ruch do obcego hosta = mozliwa eksfiltracja
    return "safe"


_KUBE_SECRET_RESOURCE = re.compile(r"(^|,)secrets?($|[,/.])")


def _kubectl_mentions_secret(tokens: list[str]) -> bool:
    """`secret`, `secrets/x`, `cm,secret`, `secrets.v1` — sekrety na liscie zasobow kubectl."""
    return any(_KUBE_SECRET_RESOURCE.search(t.lower()) for t in tokens[1:])


def _kubectl_dumps_secret(tokens: list[str]) -> bool:
    low = [t.lower() for t in tokens]
    for i, token in enumerate(low):
        name, _, inline = token.partition("=")
        if name in ("--template", "--template-file"):
            return True                             # --template bez -o oznacza go-template
        fmt = inline if name in ("-o", "--output") and inline else (
            low[i + 1] if token in ("-o", "--output") and i + 1 < len(low) else "")
        if fmt and fmt.split("=", 1)[0] in ("yaml", "json", "jsonpath", "go-template", "go-template-file",
                                            "jsonpath-file", "custom-columns"):
            return True
    return False


def _check_arguments(tokens: list[str]) -> Classification:
    """Dodatkowe reguly dla odczytow, ktore maja flagi zmieniajace stan."""
    low = [t.lower() for t in tokens]
    program = low[0]

    if program == "git":
        return _check_git(tokens)
    if program == "find":
        if any(t in ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls")
               for t in low):
            return "confirm"
    if program == "curl":
        return _check_curl(tokens)
    if program == "journalctl" and any(t.startswith(("--vacuum", "--rotate", "--flush", "--sync", "--relinquish",
                                                     "--setup-keys", "--update-catalog")) for t in low):
        return "confirm"
    if program == "sort" and any(t in ("-o",) or t.split("=", 1)[0] in ("--output", "--compress-program",
                                                                          "--files0-from") for t in low):
        return "confirm"
    if program == "tree" and ("-o" in low or any(t.startswith("-o") for t in low)):
        return "confirm"
    if program == "kubectl" and "--raw" in low:
        return "confirm"
    # kubectl get secret ... -o yaml/json (takze -o=yaml) — tresc sekretu poszlaby do LLM.
    if program == "kubectl" and _kubectl_mentions_secret(tokens) and _kubectl_dumps_secret(tokens):
        return "confirm"
    if program == "openssl" and any(t in ("-out", "-keyout") for t in low):
        return "confirm"
    return "safe"


def _is_safe_segment(tokens: list[str]) -> bool:
    low = [t.lower() for t in tokens]
    if low[0] == "git":
        return _check_git(tokens) == "safe"  # opcje globalne (-C dir) przed podkomenda
    for prefix in _safe_tokens:
        if tuple(low[:len(prefix)]) == prefix:
            return _check_arguments(tokens) == "safe"
    # `cat /proc/...` i podobne sa juz pokryte przez "cat"
    return False


def is_sensitive(text: str) -> bool:
    """
    True, gdy tekst (segment komendy albo sciezka) dotyka pliku z sekretami.
    Sprawdzany jest tez zapis po usunieciu cudzyslowow i backslashy — powloka
    czyta `/etc/sha""dow` i `.e\\nv` jak `/etc/shadow` i `.env`.
    """
    if any(pattern.search(text) for pattern in _compiled_sensitive):
        return True
    tokens = _tokens(text)
    if tokens is None:
        return True                                 # nie umiemy rozebrac — nie ufamy
    normalized = " ".join(tokens)
    return normalized != text and any(pattern.search(normalized) for pattern in _compiled_sensitive)


def classify_segment(segment: str) -> Classification:
    """Klasyfikuje jeden segment (bez operatorow sterujacych)."""
    for pattern in _compiled_forbidden:
        if pattern.search(segment):
            return "forbidden"
    if is_sensitive(segment):
        return "confirm"
    for pattern in _compiled_confirm:
        if pattern.search(segment):
            return "confirm"
    tokens = _tokens(segment)
    if not tokens:
        return "confirm"
    # Przekierowania w tokenach (np. `2>/dev/null`) nie sa czescia komendy.
    tokens = [t for t in tokens if not _HARMLESS_REDIRECT.match(t) and t not in ("2>&1", "1>&2")]
    if not tokens:
        return "confirm"
    return "safe" if _is_safe_segment(tokens) else "confirm"


def classify_command(cmd: str) -> Classification:
    """
    Klasyfikuje komende shell na jeden z trzech poziomow.

    Returns:
        "forbidden" | "confirm" | "safe"
    """
    stripped = cmd.strip()
    if not stripped:
        return "confirm"

    for pattern in _compiled_forbidden:
        if pattern.search(stripped):
            return "forbidden"

    segments, writes, dynamic = split_command(stripped)
    overall: Classification = "confirm" if (writes or dynamic) else "safe"
    for segment in segments:
        status = classify_segment(segment)
        if status == "forbidden":
            return "forbidden"
        if status == "confirm":
            overall = "confirm"
    return overall if segments else "confirm"


# ─── Zapis plikow i dostep do workspace ─────────────────────────────────────

FORBIDDEN_WRITE_PATHS = ("/etc/passwd", "/etc/shadow", "/etc/gshadow", "/boot/", "/dev/")


def classify_file_write(path: str) -> Classification:
    """
    Klasyfikuje zapis pliku. Sciezka moze byc sciezka hosta albo lokalna
    (z prefiksem HOST_ROOT). /etc/passwd, /etc/shadow, /boot, /dev — forbidden;
    kazdy inny zapis wymaga potwierdzenia.
    """
    host = runtime.to_host(path) if path.startswith("/") else path
    for candidate in {path, host}:
        for forbidden in FORBIDDEN_WRITE_PATHS:
            if candidate == forbidden.rstrip("/") or candidate.startswith(forbidden):
                return "forbidden"
    return "confirm"


# Sciezki hosta, do ktorych agent nie ma dostepu plikowego nawet do odczytu.
FORBIDDEN_HOST_PATHS = (
    "/boot", "/dev", "/proc", "/sys", "/var/spool", "/usr/bin", "/usr/sbin", "/bin", "/sbin", "/lib", "/lib64",
    "/root/.ssh", "/etc/shadow", "/etc/gshadow", "/etc/ssh/ssh_host_rsa_key", "/etc/ssh/ssh_host_ecdsa_key",
    "/etc/ssh/ssh_host_ed25519_key", "/etc/ssh/ssh_host_dsa_key",
)


_MAX_SYMLINKS = 40


def resolve_local(path: str, workspace: str | None = None) -> str:
    """
    Rozwiazuje sciezke widziana przez proces Pipe (symlinki, `..`).

    Gdy host jest zamontowany pod prefiksem (docker: /hostfs), symlinki hosta
    sa bezwzgledne wzgledem korzenia HOSTA: /hostfs/etc/nginx/sites-enabled/x
    -> /etc/nginx/sites-available/x oznacza /hostfs/etc/nginx/sites-available/x,
    a nie plik kontenera. Wynik nigdy nie wychodzi ponad prefiks (`..` na
    korzeniu zostaje na korzeniu, jak w chroot). Sciezki spoza prefiksu
    i tryb natywny — zwykle Path.resolve().
    """
    root_raw = os.path.normpath(workspace if workspace is not None else runtime.workspace_root())
    root = str(Path(root_raw).resolve())
    normalized = os.path.normpath(path)
    base = next((b for b in (root_raw, root) if normalized == b or normalized.startswith(b.rstrip("/") + "/")), None)
    if root == "/" or base is None:
        return str(Path(path).resolve())
    pending = [part for part in normalized[len(base):].split("/") if part]
    resolved: list[str] = []
    hops = 0
    while pending:
        part = pending.pop(0)
        if part == ".":
            continue
        if part == "..":
            if resolved:
                resolved.pop()
            continue
        candidate = os.path.join(root, *resolved, part)
        if os.path.islink(candidate):
            hops += 1
            if hops > _MAX_SYMLINKS:
                raise OSError(tr(f"Za duzo dowiazan symbolicznych: {path}", f"Too many symbolic links: {path}"))
            target = os.readlink(candidate)
            if target.startswith("/"):
                resolved = []
            pending = [p for p in target.split("/") if p] + pending
            continue
        resolved.append(part)
    return os.path.join(root, *resolved) if resolved else root


def validate_workspace_access(path: str, workspace: str | None = None) -> tuple[bool, str]:
    """
    Sprawdza, czy sciezka (widziana przez proces Pipe) lezy w dozwolonym workspace.

    W trybie docker workspace to /hostfs (host zamontowany read-only, /root
    zapisywalny). Natywnie workspace to `/`. Sciezki sa rozwiazywane
    (symlinki, `..`) przed sprawdzeniem, wiec `/hostfs/../etc` nie przejdzie.

    Returns:
        (dozwolone, powod)
    """
    if not path or not path.strip():
        return False, tr("Pusta sciezka", "Empty path")
    root = workspace if workspace is not None else runtime.workspace_root()
    try:
        resolved = Path(resolve_local(path, root))
        workspace_path = Path(root).resolve()
    except (ValueError, OSError):
        return False, tr(f"Nieprawidlowa sciezka: {path}", f"Invalid path: {path}")

    if not resolved.is_relative_to(workspace_path):
        return False, tr(f"Dostep poza workspace ({root}) jest zabroniony dla {path}",
                         f"Access outside the workspace ({root}) is forbidden for {path}")

    # Sciezka widziana z perspektywy hosta: /hostfs/etc/x -> /etc/x
    rel = "/" + str(resolved.relative_to(workspace_path))
    host = "/" if rel == "/." else rel
    for forbidden in FORBIDDEN_HOST_PATHS:
        if host == forbidden or host.startswith(forbidden + "/"):
            return False, tr(f"Dostep do {forbidden} jest zabroniony", f"Access to {forbidden} is forbidden")
    if re.match(r"^/home/[^/]+/\.ssh(/|$)", host):
        return False, tr("Dostep do kluczy SSH uzytkownikow jest zabroniony", "Access to users' SSH keys is forbidden")
    return True, "OK"
