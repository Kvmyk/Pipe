"""
Audyt bezpieczenstwa hosta (`/audyt`) i sygnaly wlaman z logu SSH — bez LLM.

Audyt to deterministyczne sprawdzenia konfiguracji hosta z ocena 0-100:
  SSH       logowanie hasłem, logowanie roota, klucze
  Zapora    ufw / firewalld / nftables / iptables
  Porty     bazy danych i API Dockera wystawione publicznie (Docker omija ufw!)
  Kontenery tryb uprzywilejowany, zamontowany docker.sock, siec hosta
  Konta     uid 0 poza rootem, konta bez hasla
  Aktualizacje  automatyczne aktualizacje bezpieczenstwa, oczekujace poprawki, restart
  Ochrona   fail2ban / CrowdSec przy logowaniu haslem
  Sekrety   pliki .env czytelne dla wszystkich w katalogach aplikacji z DIRECTORY
  Higiena   synchronizacja czasu, swap na malej maszynie, certyfikaty

Kazde znalezisko ma poprawke — opis i dokladna komende. Agent wykonuje ja
zwyklym narzedziem, czyli z potwierdzeniem i bezpiecznikiem (kopia, sshd -t...).
W trybie docker system plikow hosta jest tylko do odczytu, wiec komendy sa
oznaczone "na hoscie" — do wykonania w powloce serwera.

Log SSH (auth.log / secure) jest czytany przyrostowo przez czuwanie: seria
nieudanych logowan, udane logowanie haslem z adresu, ktory zgadywal hasla,
i logowanie z nowego adresu.
"""

from __future__ import annotations

import glob
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from backend.core import hostinfo, memory, runtime
from backend.core.i18n import tr

WEIGHTS = {"high": 15, "medium": 7, "low": 3}
SEVERITY_LABEL = {"high": "WYSOKIE", "medium": "SREDNIE", "low": "NISKIE"}
SEVERITY_LABEL_EN = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW"}
PUBLIC_RISKY_PORTS = {
    2375: ("Docker API", "high"), 2376: ("Docker API", "high"),
    3306: ("MySQL/MariaDB", "high"), 5432: ("PostgreSQL", "high"), 6379: ("Redis", "high"),
    27017: ("MongoDB", "high"), 9200: ("Elasticsearch", "high"), 11211: ("Memcached", "high"),
    5984: ("CouchDB", "high"), 9042: ("Cassandra", "high"), 8086: ("InfluxDB", "medium"),
    1433: ("SQL Server", "high"), 5672: ("RabbitMQ", "medium"), 15672: ("RabbitMQ management", "medium"),
    6443: ("Kubernetes API", "medium"), 10250: ("kubelet", "high"), 2379: ("etcd", "high"),
}
PIPE_CONTAINER_HINTS = ("pipe", "vps-agent")


@dataclass
class Finding:
    id: str
    severity: str            # high | medium | low
    title: str
    detail: str = ""
    fix: str = ""
    command: str = ""        # dokladna komenda poprawki (pusta = poprawka reczna)
    host_only: bool = False  # w trybie docker do wykonania w powloce hosta

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class Audit:
    findings: list[Finding] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    @property
    def score(self) -> int:
        return max(0, 100 - sum(WEIGHTS[f.severity] for f in self.findings))

    @property
    def grade(self) -> str:
        score = self.score
        return "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D" if score >= 40 else "E"

    def sorted(self) -> list[Finding]:
        order = {"high": 0, "medium": 1, "low": 2}
        return sorted(self.findings, key=lambda f: (order[f.severity], f.id))

    def render(self) -> str:
        labels = tr(SEVERITY_LABEL, SEVERITY_LABEL_EN)
        lines = [tr(f"Ocena bezpieczenstwa: {self.score}/100 ({self.grade})",
                    f"Security score: {self.score}/100 ({self.grade})")]
        for index, finding in enumerate(self.sorted(), start=1):
            lines.append(f"\n{index}. [{labels[finding.severity]}] {finding.title}")
            if finding.detail:
                lines.append(f"   {finding.detail}")
            if finding.fix:
                lines.append(tr(f"   Poprawka: {finding.fix}", f"   Fix: {finding.fix}"))
            if finding.command:
                where = tr(" (w powloce hosta — Pipe ma system plikow hosta tylko do odczytu)",
                           " (in the host shell — Pipe sees the host filesystem read-only)") if finding.host_only else ""
                lines.append(tr(f"   Komenda{where}: {finding.command}", f"   Command{where}: {finding.command}"))
        if self.passed:
            lines.append(tr("\nW porzadku: ", "\nOK: ") + "; ".join(self.passed))
        if self.unknown:
            lines.append(tr("Nie sprawdzono: ", "Not checked: ") + "; ".join(self.unknown))
        return "\n".join(lines)

    def to_data(self) -> dict[str, Any]:
        return {"score": self.score, "grade": self.grade, "findings": [f.to_dict() for f in self.sorted()],
                "passed": self.passed, "unknown": self.unknown, "text": self.render()}


def _read(path: str) -> str | None:
    local = runtime.to_local(path)
    try:
        with open(local, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _exists(path: str) -> bool:
    return os.path.lexists(runtime.to_local(path))


def _host_only() -> bool:
    return runtime.kind() != "native"


def install_command(package: str) -> str:
    """Instalacja pakietu menedzerem systemu hosta ('' gdy system nieznany)."""
    if _exists("/var/lib/dpkg/status"):
        return f"apt-get install -y {package}"
    if _exists("/etc/redhat-release") or _exists("/etc/fedora-release"):
        return f"dnf install -y {package}"
    if _exists("/etc/alpine-release"):
        return f"apk add {package}"
    if _exists("/etc/arch-release"):
        return f"pacman -S --noconfirm {package}"
    return ""


# ─── SSH ────────────────────────────────────────────────────────────────────

def sshd_settings() -> dict[str, str] | None:
    """Efektywne ustawienia globalne sshd (pierwsza wartosc wygrywa, Include w miejscu, bez blokow Match)."""
    main = _read("/etc/ssh/sshd_config")
    if main is None:
        return None
    result: dict[str, str] = {}

    def consume(text: str, depth: int = 0) -> bool:
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split(None, 1)
            key, value = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
            if key == "match":
                return False                      # dalej ustawienia warunkowe
            if key == "include" and depth < 5:
                for pattern in value.split():
                    pattern = pattern if pattern.startswith("/") else f"/etc/ssh/{pattern}"
                    for local in sorted(glob.glob(runtime.to_local(pattern))):
                        try:
                            with open(local, encoding="utf-8", errors="replace") as fh:
                                if not consume(fh.read(), depth + 1):
                                    return False
                        except OSError:
                            continue
                continue
            result.setdefault(key, value.lower())
        return True

    consume(main)
    return result


def ssh_unit() -> str:
    units = _enabled_units()
    return "sshd" if "sshd.service" in units and "ssh.service" not in units else "ssh"


def sshd_set(name: str, values: dict[str, str]) -> str:
    """
    Komenda ustawiajaca opcje sshd. sshd bierze PIERWSZA wartosc, a Ubuntu/Debian wczytuja
    sshd_config.d/*.conf na poczatku sshd_config — wiec wlasny plik 00-pipe-*.conf wygrywa
    z ustawieniami cloud-init. Bez Include: edycja sshd_config. Zawsze sshd -t przed przeladowaniem.
    """
    main = _read("/etc/ssh/sshd_config") or ""
    reload = f"sshd -t && systemctl reload {ssh_unit()}"
    if re.search(r"^\s*Include\s+/etc/ssh/sshd_config\.d/", main, re.MULTILINE | re.IGNORECASE):
        lines = " ".join(f"'{k} {v}'" for k, v in values.items())
        return f"printf '%s\\n' {lines} > /etc/ssh/sshd_config.d/00-pipe-{name}.conf && {reload}"
    edits = " ".join(f"-e 's/^#\\?{k}\\b.*/{k} {v}/'" for k, v in values.items())
    return f"sed -i {edits} /etc/ssh/sshd_config && {reload}"


def ssh_findings(audit: Audit, users: dict[str, str]) -> bool:
    """Dodaje znaleziska SSH. Zwraca True, gdy logowanie haslem jest mozliwe."""
    settings = sshd_settings()
    if settings is None:
        audit.unknown.append(tr("SSH (brak /etc/ssh/sshd_config)", "SSH (no /etc/ssh/sshd_config)"))
        return False
    port = settings.get("port", "22").split()[0]
    password = settings.get("passwordauthentication", "yes") == "yes"
    kbd = settings.get("kbdinteractiveauthentication", settings.get("challengeresponseauthentication", "yes")) == "yes"
    pam = settings.get("usepam", "no") == "yes"
    password_login = password or (kbd and pam)
    has_keys = any(_read(p) for p in ["/root/.ssh/authorized_keys"]
                   + [f"/home/{u}/.ssh/authorized_keys" for u in users if u != "root"])
    if password_login:
        fix = (tr("wylacz logowanie haslem (zostaje logowanie kluczem)",
                  "disable password login (key login stays)") if has_keys else
               tr("NAJPIERW dodaj swoj klucz publiczny do ~/.ssh/authorized_keys i sprawdz logowanie kluczem, "
                  "dopiero potem wylacz hasla — inaczej stracisz dostep",
                  "FIRST add your public key to ~/.ssh/authorized_keys and check that key login works, "
                  "only then disable passwords — otherwise you lose access"))
        command = sshd_set("password", {"PasswordAuthentication": "no", "KbdInteractiveAuthentication": "no"}) \
            if has_keys else ""
        audit.findings.append(Finding("ssh-password", "high",
                                      tr("SSH pozwala logowac sie haslem", "SSH allows password login"),
                                      tr("hasla da sie zgadywac (boty probuja non stop); klucze nie",
                                         "passwords can be guessed (bots try around the clock); keys cannot"),
                                      fix, command,
                                      _host_only()))
    else:
        audit.passed.append(tr("SSH tylko kluczem", "SSH key-only"))
    root = settings.get("permitrootlogin", "prohibit-password")
    if root == "yes":
        audit.findings.append(Finding("ssh-root", "high",
                                      tr("SSH pozwala logowac sie jako root haslem",
                                         "SSH allows root to log in with a password"),
                                      "PermitRootLogin yes", tr("zostaw roota tylko z kluczem", "allow root with a key only"),
                                      sshd_set("root", {"PermitRootLogin": "prohibit-password"}), _host_only()))
    else:
        audit.passed.append(tr(f"root przez SSH: {root}", f"root over SSH: {root}"))
    if settings.get("pubkeyauthentication", "yes") != "yes":
        audit.findings.append(Finding("ssh-pubkey", "medium",
                                      tr("Logowanie kluczem SSH jest wylaczone", "SSH key login is disabled"),
                                      "PubkeyAuthentication no",
                                      tr("wlacz klucze (bezpieczniejsze niz hasla)", "enable keys (safer than passwords)"),
                                      sshd_set("pubkey", {"PubkeyAuthentication": "yes"}), _host_only()))
    if port != "22":
        audit.passed.append(tr(f"SSH na porcie {port}", f"SSH on port {port}"))
    return password_login


# ─── Zapora, porty, kontenery ───────────────────────────────────────────────

def _enabled_units() -> set[str]:
    units = set()
    for wants in ("/etc/systemd/system/multi-user.target.wants", "/etc/systemd/system/sysinit.target.wants",
                  "/etc/systemd/system/timers.target.wants"):
        try:
            units.update(os.listdir(runtime.to_local(wants)))
        except OSError:
            continue
    return units


def firewall_active(units: set[str]) -> str:
    ufw = _read("/etc/ufw/ufw.conf") or ""
    if re.search(r"^\s*ENABLED\s*=\s*yes", ufw, re.MULTILINE):
        return "ufw"
    if "firewalld.service" in units:
        return "firewalld"
    if "nftables.service" in units and "drop" in (_read("/etc/nftables.conf") or "").lower():
        return "nftables"
    rules = _read("/etc/iptables/rules.v4") or ""
    if re.search(r":INPUT (DROP|REJECT)|-A INPUT .*-j (DROP|REJECT)", rules):
        return "iptables"
    return ""


def firewall_findings(audit: Audit, units: set[str], ssh_port: str) -> None:
    active = firewall_active(units)
    if active:
        audit.passed.append(tr(f"zapora: {active}", f"firewall: {active}"))
        return
    audit.findings.append(Finding(
        "firewall", "medium", tr("Nie widze aktywnej zapory na hoscie", "No active firewall found on the host"),
        tr("jesli zapora jest w panelu dostawcy (security group), to znalezisko mozesz zignorowac",
           "if the firewall lives in your provider's panel (security group), you can ignore this finding"),
        tr("wlacz ufw z dostepem do SSH i WWW (porty publikowane przez Dockera i tak omijaja ufw — patrz porty ponizej)",
           "enable ufw with SSH and web access (ports published by Docker bypass ufw anyway — see ports below)"),
        f"ufw allow {ssh_port}/tcp && ufw allow 80/tcp && ufw allow 443/tcp && ufw --force enable", _host_only()))


def port_findings(audit: Audit, sockets: list[Any], containers: list[Any] | None) -> None:
    public = {s.port for s in sockets if s.public and s.proto.startswith("tcp")}
    risky = False
    for port in sorted(public):
        if port not in PUBLIC_RISKY_PORTS:
            continue
        risky = True
        name, severity = PUBLIC_RISKY_PORTS[port]
        owner = next((c for c in containers or [] for p in c.ports if p.host_port == port and p.public), None)
        if owner is not None:
            where = owner.workdir or tr("katalog projektu compose", "the compose project directory")
            fix = tr(f"w pliku compose ({where}) zmien mapowanie na \"127.0.0.1:{port}:{port}\" albo usun `ports:` "
                     "(aplikacje w tej samej sieci Dockera lacza sie po nazwie uslugi), potem docker compose up -d",
                     f"in the compose file ({where}) change the mapping to \"127.0.0.1:{port}:{port}\" or remove `ports:` "
                     "(apps on the same Docker network connect by service name), then docker compose up -d")
            detail = tr(f"publikuje kontener {owner.name}; Docker dodaje reguly iptables z pominieciem ufw",
                        f"published by container {owner.name}; Docker adds iptables rules that bypass ufw")
            command = ""
        else:
            fix = tr("ustaw nasluchiwanie na 127.0.0.1 (bind-address / listen_addresses / bind) albo zablokuj port w zaporze",
                     "listen on 127.0.0.1 (bind-address / listen_addresses / bind) or block the port in the firewall")
            detail = tr("usluga nasluchuje na wszystkich interfejsach (jesli publikuje ja kontener Dockera, "
                        "ufw jej nie zablokuje — zmien mapowanie portu w compose)",
                        "the service listens on all interfaces (if a Docker container publishes it, "
                        "ufw will not block it — change the port mapping in compose)")
            command = f"ufw deny {port}/tcp"
        audit.findings.append(Finding(f"port-{port}", severity, tr(f"{name} jest dostepny z internetu (port {port})", f"{name} is reachable from the internet (port {port})"),
                                      detail, fix, command, _host_only() and bool(command)))
    if not risky:
        audit.passed.append(tr("brak publicznych portow baz danych i API Dockera", "no public database or Docker API ports"))


def container_findings(audit: Audit, containers: list[Any] | None) -> None:
    if containers is None:
        audit.unknown.append(tr("kontenery (brak dostepu do Dockera)", "containers (no access to Docker)"))
        return
    for c in containers:
        if any(h in c.name.lower() for h in PIPE_CONTAINER_HINTS):
            continue   # Pipe z docker.sock — swiadoma decyzja (docs/security.md)
        socket = any(src in ("/var/run/docker.sock", "/run/docker.sock") for src, _ in getattr(c, "mounts", []))
        if getattr(c, "privileged", False):
            audit.findings.append(Finding(f"privileged-{c.name}", "medium",
                                          tr(f"Kontener {c.name} dziala w trybie uprzywilejowanym",
                                             f"Container {c.name} runs in privileged mode"),
                                          tr("privileged: true = pelny dostep do hosta",
                                             "privileged: true = full access to the host"),
                                          tr("usun privileged: true z compose i dodaj tylko potrzebne cap_add",
                                             "remove privileged: true from compose and add only the cap_add you need")))
        if socket:
            audit.findings.append(Finding(f"docker-sock-{c.name}", "medium",
                                          tr(f"Kontener {c.name} ma dostep do docker.sock",
                                             f"Container {c.name} has access to docker.sock"),
                                          tr("docker.sock = root na hoscie; przejecie kontenera = przejecie serwera",
                                             "docker.sock = root on the host; a compromised container = a compromised server"),
                                          tr("jesli to potrzebne (Traefik, Portainer), zamontuj :ro albo uzyj docker socket proxy",
                                             "if it is needed (Traefik, Portainer), mount it :ro or use a docker socket proxy")))
        if getattr(c, "network_mode", "") == "host":
            audit.findings.append(Finding(f"host-network-{c.name}", "low",
                                          tr(f"Kontener {c.name} uzywa sieci hosta",
                                             f"Container {c.name} uses the host network"),
                                          tr("wszystkie jego porty sa portami hosta", "all its ports are host ports"),
                                          tr("jesli nie jest to konieczne, uzyj sieci bridge i publikuj tylko potrzebne porty",
                                             "if not required, use a bridge network and publish only the ports you need")))


# ─── Konta, aktualizacje, ochrona, sekrety, higiena ─────────────────────────

def account_findings(audit: Audit) -> None:
    passwd = _read("/etc/passwd") or ""
    extra_root = [l.split(":")[0] for l in passwd.splitlines()
                  if len(l.split(":")) > 3 and l.split(":")[2] == "0" and l.split(":")[0] != "root"]
    if extra_root:
        audit.findings.append(Finding("uid0", "high",
                                      tr("Konto z uprawnieniami roota (uid 0) poza rootem: ",
                                         "Account with root privileges (uid 0) other than root: ")
                                      + ", ".join(extra_root),
                                      tr("typowy slad wlamania albo stara pozostalosc",
                                         "a classic sign of a break-in, or an old leftover"),
                                      tr("sprawdz, kto i kiedy je dodal (/zmiany, last), potem usun albo zmien uid",
                                         "check who added it and when (/changes, last), then remove it or change the uid")))
    shadow = _read("/etc/shadow")
    if shadow is None:
        audit.unknown.append(tr("hasla kont (brak dostepu do /etc/shadow)", "account passwords (no access to /etc/shadow)"))
    else:
        # Tresc shadow nie opuszcza procesu — zapisujemy wylacznie nazwy kont z pustym haslem.
        empty = [l.split(":")[0] for l in shadow.splitlines() if len(l.split(":")) > 1 and l.split(":")[1] == ""]
        if empty:
            audit.findings.append(Finding("empty-password", "high",
                                          tr("Konta bez hasla: ", "Accounts without a password: ") + ", ".join(empty),
                                          tr("puste pole hasla w /etc/shadow = logowanie bez hasla (lokalnie/su)",
                                             "empty password field in /etc/shadow = login without a password (local/su)"),
                                          tr("zablokuj haslo", "lock the password"), " && ".join(f"passwd -l {u}" for u in empty), _host_only()))
    if not extra_root:
        audit.passed.append(tr("tylko root ma uid 0", "only root has uid 0"))


def update_findings(audit: Audit, units: set[str], packages: dict[str, str]) -> None:
    debian = _exists("/var/lib/dpkg/status")
    if debian:
        auto = _read("/etc/apt/apt.conf.d/20auto-upgrades") or ""
        enabled = "unattended-upgrades" in packages and re.search(r'Unattended-Upgrade\s+"1"', auto)
        if enabled:
            audit.passed.append(tr("automatyczne aktualizacje bezpieczenstwa", "automatic security updates"))
        else:
            audit.findings.append(Finding(
                "auto-updates", "medium",
                tr("Automatyczne aktualizacje bezpieczenstwa sa wylaczone", "Automatic security updates are off"),
                tr("luki w systemie czekaja na reczny apt upgrade", "system vulnerabilities wait for a manual apt upgrade"),
                tr("zainstaluj i wlacz unattended-upgrades", "install and enable unattended-upgrades"),
                "apt-get install -y unattended-upgrades && dpkg-reconfigure -f noninteractive unattended-upgrades",
                _host_only()))
    elif "dnf-automatic.timer" in units or "dnf-automatic-install.timer" in units:
        audit.passed.append(tr("automatyczne aktualizacje (dnf-automatic)", "automatic updates (dnf-automatic)"))
    else:
        audit.unknown.append(tr("automatyczne aktualizacje (system inny niz Debian/Ubuntu)", "automatic updates (not a Debian/Ubuntu system)"))
    notifier = _read("/var/lib/update-notifier/updates-available") or ""
    match = re.search(r"(\d+)\s+(?:of these\s+)?updates?\s+(?:are|is)\s+(?:a\s+)?(?:standard\s+)?security", notifier)
    if match and int(match.group(1)):
        audit.findings.append(Finding("security-updates", "medium",
                                      tr(f"Czeka {match.group(1)} aktualizacji bezpieczenstwa",
                                         f"{match.group(1)} security updates pending"),
                                      "", tr("zainstaluj aktualizacje (potem ewentualny restart)",
                                             "install the updates (a reboot may follow)"),
                                      "apt-get update && apt-get upgrade -y", _host_only()))
    if _read("/run/reboot-required") is not None or _read("/var/run/reboot-required") is not None:
        audit.findings.append(Finding("reboot", "low",
                                      tr("Serwer czeka na restart po aktualizacji jadra/bibliotek",
                                         "The server needs a reboot after a kernel/library update"),
                                      tr("poprawki bezpieczenstwa jadra dzialaja dopiero po restarcie",
                                         "kernel security fixes only take effect after a reboot"),
                                      tr("zaplanuj restart w oknie serwisowym", "schedule a reboot in a maintenance window")))


def protection_findings(audit: Audit, units: set[str], packages: dict[str, str], password_login: bool) -> None:
    protected = any(name in packages for name in ("fail2ban", "crowdsec")) or \
        any(u.startswith(("fail2ban", "crowdsec")) for u in units)
    if protected:
        audit.passed.append(tr("ochrona przed zgadywaniem hasel (fail2ban/CrowdSec)", "brute-force protection (fail2ban/CrowdSec)"))
        return
    audit.findings.append(Finding(
        "bruteforce", "medium" if password_login else "low",
        tr("Brak ochrony przed zgadywaniem hasel (fail2ban/CrowdSec)", "No brute-force protection (fail2ban/CrowdSec)"),
        tr("przy logowaniu haslem to realne ryzyko", "with password login this is a real risk") if password_login
        else tr("przy logowaniu tylko kluczem ryzyko jest male", "with key-only login the risk is small"),
        tr("zainstaluj fail2ban (skill: fail2ban-ssh)", "install fail2ban (skill: fail2ban-ssh)"), install_command("fail2ban"), _host_only()))


def secret_file_findings(audit: Audit) -> None:
    try:
        entries = [e for e in memory.load_directory() if e.kind in ("app", "compose", "repo")]
    except OSError:
        entries = []
    exposed = []
    for entry in entries[:100]:
        for local in glob.glob(runtime.to_local(entry.path.rstrip("/") + "/*.env")) + \
                glob.glob(runtime.to_local(entry.path.rstrip("/") + "/.env")):
            try:
                if os.stat(local).st_mode & 0o004:
                    exposed.append(runtime.to_host(local))
            except OSError:
                continue
    if exposed:
        audit.findings.append(Finding(
            "env-readable", "medium",
            tr("Pliki z sekretami czytelne dla wszystkich kont: ", "Secret files readable by every account: ")
            + ", ".join(exposed[:5]),
            tr("kazdy uzytkownik i proces na serwerze przeczyta hasla z tych plikow",
               "any user or process on the server can read the passwords in these files"),
            tr("ogranicz prawa do wlasciciela", "restrict permissions to the owner"), "chmod 600 " + " ".join(exposed[:10]), _host_only()))
    elif entries:
        audit.passed.append(tr("pliki .env aplikacji nie sa czytelne dla wszystkich", "app .env files are not world-readable"))


def hygiene_findings(audit: Audit, units: set[str], proc: str | None) -> None:
    if not any(u.startswith(("systemd-timesyncd", "chrony", "ntp", "ntpsec", "openntpd")) for u in units):
        audit.findings.append(Finding("timesync", "low",
                                      tr("Nie widze synchronizacji czasu (timesyncd/chrony)",
                                         "No time synchronisation found (timesyncd/chrony)"),
                                      tr("rozjechany zegar psuje certyfikaty, logi i tokeny",
                                         "a drifting clock breaks certificates, logs and tokens"),
                                      tr("wlacz synchronizacje czasu", "enable time synchronisation"), "timedatectl set-ntp true", _host_only()))
    mem = hostinfo.memory(proc)
    if mem and not mem.swap_total_mb and mem.total_mb < 2048:
        audit.findings.append(Finding("swap", "low",
                                      tr(f"Brak swapu przy {mem.total_mb} MB RAM", f"No swap with {mem.total_mb} MB RAM"),
                                      tr("przy skoku pamieci kernel zabije proces (OOM) zamiast zwolnic",
                                         "on a memory spike the kernel kills a process (OOM) instead of slowing down"),
                                      tr("dodaj swap (skill: swap)", "add swap (skill: swap)")))


def cert_findings(audit: Audit, report: Any, cert_days: int) -> None:
    if report is None:
        return
    for cert in report.certs:
        if cert.rejected:
            audit.findings.append(Finding(f"cert-{cert.domain}", "medium",
                                          tr(f"Certyfikat {cert.domain} jest nieprawidlowy",
                                             f"Certificate for {cert.domain} is invalid"),
                                          cert.error, tr("odnow certyfikat (certbot renew / restart proxy)",
                                                         "renew the certificate (certbot renew / restart the proxy)")))
        elif cert.days_left is not None and cert.days_left <= cert_days:
            audit.findings.append(Finding(f"cert-{cert.domain}", "medium",
                                          tr(f"Certyfikat {cert.domain} wygasa za {int(cert.days_left)} dni",
                                             f"Certificate for {cert.domain} expires in {int(cert.days_left)} days"),
                                          "", tr("sprawdz automatyczne odnawianie (certbot.timer, proxy)",
                                                 "check automatic renewal (certbot.timer, proxy)")))


def run_audit(*, containers: list[Any] | None, sockets: list[Any], report: Any = None, cert_days: int = 14,
              proc: str | None = None) -> Audit:
    """Pelny audyt (synchronicznie — same odczyty plikow)."""
    from backend.core import snapshots

    audit = Audit()
    units = _enabled_units()
    users = snapshots.users()
    packages = snapshots.packages()
    password_login = ssh_findings(audit, users)
    settings = sshd_settings() or {}
    firewall_findings(audit, units, settings.get("port", "22").split()[0])
    port_findings(audit, sockets, containers)
    container_findings(audit, containers)
    account_findings(audit)
    update_findings(audit, units, packages)
    protection_findings(audit, units, packages, password_login)
    secret_file_findings(audit)
    hygiene_findings(audit, units, proc)
    cert_findings(audit, report, cert_days)
    return audit


async def audit_now(report: Any = None, cert_days: int = 14) -> Audit:
    import asyncio

    from backend.core import infra

    containers = await infra.docker_containers()
    sockets = await asyncio.to_thread(hostinfo.sockets)
    return await asyncio.to_thread(run_audit, containers=containers, sockets=sockets, report=report,
                                   cert_days=cert_days)


# ─── Log SSH: wlamania i nowe logowania ─────────────────────────────────────

AUTH_LOGS = ("/var/log/auth.log", "/var/log/secure")
_FAILED = re.compile(r"sshd\[\d+\]: (?:Failed password|Invalid user|Failed publickey|"
                     r"Connection closed by (?:invalid|authenticating) user).*?from ([0-9a-fA-F:.]+)")
_ACCEPTED = re.compile(r"sshd\[\d+\]: Accepted (\w+) for (\S+) from ([0-9a-fA-F:.]+)")
MAX_READ = 5 * 1024 * 1024
MAX_KNOWN_LOGINS = 300


@dataclass
class AuthEvents:
    failures: Counter = field(default_factory=Counter)      # ip -> liczba
    accepted: list[tuple[str, str, str]] = field(default_factory=list)   # (metoda, uzytkownik, ip)


def parse_auth(text: str) -> AuthEvents:
    events = AuthEvents()
    for line in text.splitlines():
        match = _FAILED.search(line)
        if match:
            events.failures[match.group(1)] += 1
            continue
        match = _ACCEPTED.search(line)
        if match:
            events.accepted.append((match.group(1), match.group(2), match.group(3)))
    return events


def read_auth_increment(state: dict[str, Any]) -> AuthEvents | None:
    """Nowe linie logu SSH od ostatniego odczytu (offset w `state`). None — brak logu."""
    for path in AUTH_LOGS:
        local = runtime.to_local(path)
        try:
            size = os.path.getsize(local)
        except OSError:
            continue
        key = f"auth_offset:{path}"
        offset = state.get(key)
        if offset is None or offset > size:      # pierwszy odczyt albo rotacja logu
            offset = size if offset is None else 0
        start = max(offset, size - MAX_READ)
        try:
            with open(local, "rb") as fh:
                fh.seek(start)
                text = fh.read(size - start).decode("utf-8", errors="replace")
        except OSError:
            return None
        state[key] = size
        return parse_auth(text)
    return None


@dataclass
class AuthWatch:
    """Okno czasowe nieudanych logowan i znane pary (uzytkownik, adres)."""
    window: list[tuple[float, Counter]] = field(default_factory=list)
    suspects: set[str] = field(default_factory=set)

    def update(self, events: AuthEvents, now: float | None = None, window_s: float = 600) -> Counter:
        now = now if now is not None else time.time()
        self.window = [(t, c) for t, c in self.window if now - t <= window_s] + [(now, events.failures)]
        total: Counter = Counter()
        for _, counter in self.window:
            total.update(counter)
        self.suspects |= {ip for ip, n in total.items() if n >= 5}
        return total


def auth_findings(events: AuthEvents, watch: AuthWatch, state: dict[str, Any], *, threshold: int,
                  notify_logins: bool, now: float | None = None) -> list[Any]:
    """Znaleziska czuwania z przyrostu logu: seria nieudanych logowan (trwale), podejrzane i nowe logowania."""
    from backend.core.watch import Finding as WatchFinding

    findings = []
    total = watch.update(events, now)
    count = sum(total.values())
    if threshold and count >= threshold:
        top = ", ".join(f"{ip} ({n})" for ip, n in total.most_common(3))
        findings.append(WatchFinding("auth:ssh", "warning",
                                     tr(f"Seria nieudanych logowan SSH: {count} w 10 min",
                                        f"Burst of failed SSH logins: {count} in 10 min"),
                                     tr(f"najczesciej: {top}", f"top sources: {top}")))
    known = set(state.get("known_logins", []))
    first_run = "known_logins" not in state
    for method, user, ip in events.accepted:
        pair = f"{user}@{ip}"
        if method in ("password", "keyboard-interactive") and ip in watch.suspects:
            findings.append(WatchFinding(f"auth:breach:{pair}", "critical",
                                         tr(f"Udane logowanie SSH HASLEM na {user} z adresu, ktory zgadywal hasla ({ip})",
                                            f"Successful SSH PASSWORD login as {user} from an address that was guessing passwords ({ip})"),
                                         tr("mozliwe wlamanie — sprawdz /zmiany i `last`, zmien haslo, zablokuj adres",
                                            "possible break-in — check /changes and `last`, change the password, block the address"),
                                         transient=True))
        elif notify_logins and not first_run and pair not in known:
            findings.append(WatchFinding(f"auth:login:{pair}", "warning",
                                         tr(f"Logowanie SSH z nowego adresu: {user} z {ip} ({method})",
                                            f"SSH login from a new address: {user} from {ip} ({method})"),
                                         tr("pierwsze logowanie tej pary uzytkownik/adres — jesli to nie Ty, dzialaj",
                                            "first login for this user/address pair — if it wasn't you, act now"),
                                         transient=True))
        known.add(pair)
    state["known_logins"] = sorted(known)[-MAX_KNOWN_LOGINS:]
    return findings


# ─── Straznik: szybkie wykrywanie zmian bezpieczenstwa ──────────────────────

SENTINEL_CONFIGS = ("/etc/sudoers", "/etc/sudoers.d/*", "/etc/ssh/sshd_config", "/etc/ssh/sshd_config.d/*",
                    "/etc/passwd", "/etc/group", "/etc/ld.so.preload", "/etc/pam.d/sshd", "/etc/pam.d/common-auth")


def sentinel_state() -> dict[str, dict[str, str]]:
    """Maly wycinek stanu dla czuwania co WATCH_INTERVAL: konta, klucze SSH, SUID, konfiguracje uwierzytelniania."""
    from backend.core import snapshots

    configs: dict[str, str] = {}
    for pattern in SENTINEL_CONFIGS:
        for path in (snapshots._glob_host(pattern) if "*" in pattern else [pattern]):
            text = snapshots._read_host(path)
            if text is not None:
                configs[path] = snapshots._digest(text)
    return {"users": snapshots.users(), "ssh_keys": snapshots.ssh_keys(), "suid": snapshots.suid(),
            "configs": configs}


def _is_critical(change: Any) -> bool:
    """Nowe konto z uid 0, nowy klucz SSH, nowy program SUID, ld.so.preload — klasyczne slady wlamania."""
    if change.key == "/etc/ld.so.preload":
        return True
    if change.section == "users":
        return change.kind != "removed" and "uid=0 " in change.detail.split(" -> ")[-1] + " "
    if change.section == "ssh_keys":
        return change.kind == "added" or "+ " in change.detail
    if change.section == "suid":
        return change.kind == "added"
    return False


def sentinel_findings(previous: dict[str, dict[str, str]] | None, current: dict[str, dict[str, str]],
                      pipe_changed: set[str] = frozenset()) -> list[Any]:
    """Alerty ze zmian bezpieczenstwa. Pomija sciezki zmienione przez Pipe (dziennik) — uzytkownik je zatwierdzil."""
    from backend.core import snapshots
    from backend.core.watch import Finding as WatchFinding

    if previous is None:
        return []
    findings = []
    for change in snapshots.diff(previous, current):
        if change.key in pipe_changed:
            continue
        text = snapshots.describe(change).split("\n", 1)[0]
        critical = _is_critical(change)
        detail = change.detail.replace("\n", "; ")[:300] if change.section in ("ssh_keys", "users") else \
            tr("nie przez Pipe — jesli to nie Ty, sprawdz /zmiany i `last`",
               "not made through Pipe — if it wasn't you, check /changes and `last`")
        findings.append(WatchFinding(f"security:{change.section}:{change.key}", "critical" if critical else "warning",
                                     tr(f"Zmiana bezpieczenstwa: {text}", f"Security change: {text}"), detail, transient=True))
    return findings
