"""
Pierwsze uruchomienie `pipe web`: gdzie ma dzialac Pipe i jak sie z nim laczyc.

Dwie drogi, obie z tego komputera, bez pomocy uzytkownika w terminalu:

- **lokalnie** — `docker compose up` backendu z tego repozytorium (Linux: pelny tryb docker, Pipe zarzadza tym
  komputerem; Windows i macOS: `docker-compose.desktop.yml`, Pipe pracuje we wlasnym kontenerze Docker Desktop);
- **na serwerze** — SSH (paramiko) z haslem albo kluczem: klon repozytorium do /opt/pipe, `install-server.sh`
  bez pytan (LLM_PROVIDER=none — klucz modelu dodaje sie potem w `pipe web`), odczyt AGENT_TOKEN.

Dane logowania zostaja w tym procesie: haslo sluzy tylko do tej jednej sesji SSH (i `sudo -S`), nie trafia na dysk,
do backendu ani do modelu. Po hasle Pipe dopisuje na serwerze wlasny klucz (~/.pipe/keys) i kolejne polaczenia
(tunel OpenSSH jak w `pipe --host`) ida juz kluczem. Klucz hosta: jak `StrictHostKeyChecking=accept-new` tunelu —
nowy trafia do ~/.ssh/known_hosts (z odciskiem w postepie), zmieniony przerywa instalacje.

Zapis: ~/.pipe/app.json (0600) — rodzaj polaczenia, adres, sciezka klucza, token backendu serwera.
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

APP_DIR = Path(os.getenv("PIPE_HOME", "") or Path.home() / ".pipe")
REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_URL = "https://github.com/Kvmyk/pipe.git"
REMOTE_DIR = "/opt/pipe"
BACKEND_PORT = 7379
TOKEN_MARK = "@@PIPE_TOKEN@@"
LOCAL_WAIT = 180.0                # pierwsze `up` pobiera obraz
REMOTE_TIMEOUT = 45 * 60          # instalacja Dockera i obrazu na wolnym serwerze
HOST_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
                     r"|\d{1,3}(?:\.\d{1,3}){3}|[0-9A-Fa-f:]{2,39})$")
USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\r")

Progress = Callable[[str], None]


class SetupError(Exception):
    """Blad do pokazania uzytkownikowi (bez sladu stosu)."""


def _tr(lang: str, pl: str, en: str) -> str:
    return pl if lang == "pl" else en


# ─── Zapis ───────────────────────────────────────────────────────────────────

def _app_file() -> Path:
    return APP_DIR / "app.json"


def load() -> dict:
    try:
        data = json.loads(_app_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict) -> None:
    path = _app_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.replace(tmp, path)


def saved_connection() -> dict | None:
    conn = load().get("connection")
    return conn if isinstance(conn, dict) and conn.get("kind") in ("local", "server") else None


def remember(conn: dict) -> None:
    data = load()
    data["connection"] = conn
    save(data)


def label(conn: dict, lang: str = "en") -> str:
    if conn.get("kind") == "local":
        return _tr(lang, "ten komputer", "this computer")
    return f"{conn.get('user', 'root')}@{conn.get('host', '')}"


def ssh_target(conn: dict) -> str:
    return f"{conn.get('user') or 'root'}@{conn['host']}"


# ─── Lokalnie (Docker) ───────────────────────────────────────────────────────

def has_paramiko() -> bool:
    return importlib.util.find_spec("paramiko") is not None


def local_problem(lang: str = "en") -> str:
    """'' gdy Pipe da sie uruchomic na tym komputerze; inaczej powod zwyklymi slowami."""
    if not (REPO_ROOT / "backend" / "docker-compose.yml").is_file():
        return _tr(lang, "Brak katalogu backend obok aplikacji — zainstaluj Pipe z repozytorium.",
                   "The backend folder is missing next to the app — install Pipe from the repository.")
    if not shutil.which("docker"):
        return _tr(lang, "Na tym komputerze nie ma Dockera. Zainstaluj Docker Desktop (docker.com) i uruchom go.",
                   "Docker is not installed on this computer. Install Docker Desktop (docker.com) and start it.")
    try:
        info = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True,
                              timeout=20)
        compose = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        info = compose = None
    if info is None or info.returncode != 0:
        return _tr(lang, "Docker jest zainstalowany, ale nie dziala. Uruchom Docker Desktop i sprobuj jeszcze raz.",
                   "Docker is installed but not running. Start Docker Desktop and try again.")
    if compose is None or compose.returncode != 0:
        return _tr(lang, "Brakuje docker compose — zaktualizuj Dockera.", "docker compose is missing — update Docker.")
    return ""


def _env_value(name: str) -> str:
    try:
        for line in (REPO_ROOT / "backend" / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def local_token() -> str:
    return _env_value("AGENT_TOKEN")


def _compose_files() -> list[str]:
    files = ["-f", "docker-compose.yml"]
    if platform.system() != "Linux":
        # Docker Desktop: bez montowania systemu plikow hosta — Pipe pracuje we wlasnym kontenerze
        files += ["-f", "docker-compose.desktop.yml"]
    return files


def _stream(cmd: list[str], progress: Progress, *, cwd: Path, env: dict | None = None, timeout: float = 1800) -> int:
    process = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
    deadline = time.monotonic() + timeout
    assert process.stdout is not None
    for line in process.stdout:
        line = ANSI_RE.sub("", line).rstrip()
        if line:
            progress(line)
        if time.monotonic() > deadline:
            process.kill()
            break
    return process.wait()


def port_open(port: int = BACKEND_PORT, host: str = "127.0.0.1") -> bool:
    """
    Czy backend odpowiada. Samo polaczenie TCP nie wystarczy: Docker Desktop przyjmuje je na opublikowanym porcie,
    zanim proces w kontenerze zacznie sluchac (i zrywa po chwili) — dlatego czekamy na pierwsza linie odpowiedzi
    (dowolnej, takze bledu autoryzacji).
    """
    try:
        with socket.create_connection((host, port), timeout=2) as sock:
            sock.settimeout(3)
            sock.sendall(b'{"command": "view", "session_id": "pipe-probe"}\n')
            return sock.recv(1) != b""
    except OSError:
        return False


def start_local(progress: Progress, lang: str = "en") -> dict:
    """Uruchamia backend w Dockerze na tym komputerze (idempotentnie) i czeka, az odpowie."""
    problem = local_problem(lang)
    if problem:
        raise SetupError(problem)
    backend = REPO_ROOT / "backend"
    env = dict(os.environ)
    if not _env_value("LLM_PROVIDER") and not _env_value("LLM_BASE_URL"):
        progress(_tr(lang, "Tworze konfiguracje (klucz modelu dodasz za chwile w aplikacji)…",
                     "Creating the configuration (you will add the model key in the app in a moment)…"))
        code = _stream([sys.executable, "-m", "backend.configure", "--from-env"], progress, cwd=REPO_ROOT,
                       env={**env, "LLM_PROVIDER": "none", "PIPE_LANG": "pl" if lang == "pl" else "en"}, timeout=120)
        if code != 0:
            raise SetupError(_tr(lang, "Nie udalo sie zapisac backend/.env.", "Could not write backend/.env."))
    (backend / "data").mkdir(exist_ok=True)
    try:
        env["PIPE_VERSION"] = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    compose = ["docker", "compose", *_compose_files()]
    if not port_open():
        progress(_tr(lang, "Pobieram obraz Pipe…", "Pulling the Pipe image…"))
        _stream([*compose, "pull", "vps-agent"], progress, cwd=backend, env=env)
        progress(_tr(lang, "Uruchamiam kontener…", "Starting the container…"))
        if _stream([*compose, "up", "-d", "vps-agent"], progress, cwd=backend, env=env) != 0:
            progress(_tr(lang, "Obrazu nie ma w rejestrze — buduje go ze zrodel (kilka minut)…",
                         "The image is not in the registry — building it from source (a few minutes)…"))
            if _stream([*compose, "up", "-d", "--build", "vps-agent"], progress, cwd=backend, env=env) != 0:
                raise SetupError(_tr(lang, "docker compose nie uruchomil Pipe — szczegoly wyzej.",
                                     "docker compose did not start Pipe — details above."))
    deadline = time.monotonic() + LOCAL_WAIT
    while not port_open():
        if time.monotonic() > deadline:
            raise SetupError(_tr(lang, "Pipe nie odpowiada na porcie 7379. Logi: cd backend && docker compose logs vps-agent",
                                 "Pipe does not respond on port 7379. Logs: cd backend && docker compose logs vps-agent"))
        time.sleep(1)
    progress(_tr(lang, "Pipe dziala na tym komputerze.", "Pipe runs on this computer."))
    return {"kind": "local"}


# ─── Na serwerze (SSH) ───────────────────────────────────────────────────────

def validate_server(params: dict, lang: str = "en") -> tuple[str, int, str]:
    host = str(params.get("host") or "").strip()
    user = str(params.get("user") or "root").strip()
    if "@" in host and not params.get("user"):
        user, host = host.split("@", 1)
    host = host.strip("[]")
    try:
        port = int(params.get("port") or 22)
    except (TypeError, ValueError):
        port = 0
    if not HOST_RE.match(host):
        raise SetupError(_tr(lang, "Podaj adres serwera: IP (np. 203.0.113.7) albo nazwe (np. vps.example.com).",
                             "Enter the server address: an IP (e.g. 203.0.113.7) or a name (e.g. vps.example.com)."))
    if not USER_RE.match(user):
        raise SetupError(_tr(lang, "Nieprawidlowa nazwa uzytkownika SSH.", "Invalid SSH user name."))
    if not 1 <= port <= 65535:
        raise SetupError(_tr(lang, "Port SSH to liczba od 1 do 65535 (zwykle 22).",
                             "The SSH port is a number from 1 to 65535 (usually 22)."))
    return host, port, user


def remote_script(lang: str = "en", repo: str = REPO_URL, directory: str = REMOTE_DIR) -> str:
    """Skrypt na serwerze (jako root): klon + install-server.sh bez pytan; na koncu token za znacznikiem."""
    lang = "pl" if lang == "pl" else "en"
    q = shlex.quote
    return f"""set -e
exec </dev/null
DIR={q(directory)}
t() {{ case {lang} in pl) printf '%s' "$1" ;; *) printf '%s' "$2" ;; esac; }}
configured() {{ [ -f "$DIR/backend/.env" ] && grep -qE '^LLM_(PROVIDER|BASE_URL)=.+' "$DIR/backend/.env"; }}
if configured && (exec 3<>/dev/tcp/127.0.0.1/{BACKEND_PORT}) 2>/dev/null; then
  echo "[pipe] $(t 'Pipe juz dziala na tym serwerze - lacze sie.' 'Pipe already runs on this server - connecting.')"
else
  if [ ! -d "$DIR/.git" ]; then
    if ! command -v git >/dev/null 2>&1; then
      echo "[pipe] $(t 'Instaluje git' 'Installing git')"
      if command -v apt-get >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git
      elif command -v dnf >/dev/null 2>&1; then dnf install -y -q git
      elif command -v yum >/dev/null 2>&1; then yum install -y -q git
      elif command -v apk >/dev/null 2>&1; then apk add --no-cache git
      else echo "[pipe] $(t 'Brak gita i menedzera pakietow.' 'No git and no package manager.')"; exit 1
      fi
    fi
    echo "[pipe] $(t 'Klonuje Pipe do' 'Cloning Pipe into') $DIR"
    git clone --depth 1 {q(repo)} "$DIR"
  fi
  if configured; then
    bash "$DIR/scripts/install-server.sh" </dev/null
  else
    LLM_PROVIDER=none bash "$DIR/scripts/install-server.sh" --non-interactive --lang {lang} </dev/null
  fi
fi
printf '\\n{TOKEN_MARK}%s\\n' "$(sed -n 's/^AGENT_TOKEN=//p' "$DIR/backend/.env" | head -n1)"
"""


def _known_hosts() -> Path:
    return Path.home() / ".ssh" / "known_hosts"


def _fingerprint(key) -> str:
    import base64
    import hashlib
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def _accept_new_policy(progress: Progress, lang: str):
    import paramiko

    class AcceptNew(paramiko.MissingHostKeyPolicy):
        """Nowy serwer: zapisz klucz (jak accept-new tunelu OpenSSH); zmieniony klucz paramiko odrzuca sam."""

        def missing_host_key(self, client, hostname, key):
            progress(_tr(lang, f"Nowy serwer, odcisk klucza: {_fingerprint(key)}",
                         f"New server, key fingerprint: {_fingerprint(key)}"))
            path = _known_hosts()
            try:
                path.parent.mkdir(mode=0o700, exist_ok=True)
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(f"{hostname} {key.get_name()} {key.get_base64()}\n")
            except OSError:
                pass

    return AcceptNew()


def _run(client, command: str, *, stdin: str = "", on_line: Progress | None = None,
         timeout: float = 120, secret: str = "") -> tuple[int, str]:
    """Komenda przez SSH: (kod, wyjscie). Wyjscie bez kolorow; haslo (gdyby wrocilo w wyjsciu) zamaskowane."""
    channel = client.get_transport().open_session()
    channel.set_combined_stderr(True)
    channel.exec_command(command)
    if stdin:
        channel.sendall(stdin.encode("utf-8"))
    channel.shutdown_write()
    deadline = time.monotonic() + timeout
    buffer, output = "", []
    while True:
        if channel.recv_ready():
            buffer += channel.recv(65536).decode("utf-8", "replace")
            *lines, buffer = buffer.split("\n")
            for line in lines:
                line = ANSI_RE.sub("", line).rstrip()
                if secret:
                    line = line.replace(secret, "***")
                output.append(line)
                if on_line and line and not line.startswith(TOKEN_MARK):
                    on_line(line)
            continue
        if channel.exit_status_ready() and not channel.recv_ready():
            break
        if time.monotonic() > deadline:
            channel.close()
            raise TimeoutError(command[:40])
        time.sleep(0.05)
    if buffer:
        output.append(ANSI_RE.sub("", buffer).rstrip())
    return channel.recv_exit_status(), "\n".join(output)


def _own_key(host: str, user: str, port: int):
    """Klucz Pipe dla tego serwera w ~/.pipe/keys (tworzony raz): (sciezka, linia klucza publicznego)."""
    import paramiko

    keys = APP_DIR / "keys"
    keys.mkdir(parents=True, exist_ok=True)
    path = keys / re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{user}@{host}_{port}")
    if path.is_file():
        key = paramiko.RSAKey.from_private_key_file(str(path))
    else:
        key = paramiko.RSAKey.generate(3072)
        key.write_private_key_file(str(path))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return path, f"{key.get_name()} {key.get_base64()} pipe@{socket.gethostname() or 'pipe'}"


def deploy_server(params: dict, progress: Progress, lang: str = "en") -> dict:
    """Stawia (albo odnajduje) Pipe na serwerze przez SSH i zwraca polaczenie do zapisania."""
    if not has_paramiko():
        raise SetupError(_tr(lang, f"Brakuje pakietu paramiko: {sys.executable} -m pip install paramiko",
                             f"The paramiko package is missing: {sys.executable} -m pip install paramiko"))
    import paramiko

    host, port, user = validate_server(params, lang)
    password = str(params.get("password") or "")
    key_file = os.path.expanduser(str(params.get("key") or "").strip())
    if key_file and not os.path.isfile(key_file):
        raise SetupError(_tr(lang, f"Nie ma pliku klucza {key_file}.", f"There is no key file {key_file}."))
    client = paramiko.SSHClient()
    if _known_hosts().is_file():
        try:
            client.load_system_host_keys(str(_known_hosts()))
        except (OSError, paramiko.SSHException):
            pass
    client.set_missing_host_key_policy(_accept_new_policy(progress, lang))
    progress(_tr(lang, f"Lacze sie z {user}@{host}:{port}…", f"Connecting to {user}@{host}:{port}…"))
    try:
        client.connect(host, port=port, username=user, password=password or None, key_filename=key_file or None,
                       look_for_keys=not (password or key_file), allow_agent=not password, timeout=20,
                       banner_timeout=20, auth_timeout=30)
    except paramiko.BadHostKeyException:
        raise SetupError(_tr(lang, "Klucz tego serwera jest inny niz zapamietany w ~/.ssh/known_hosts. Jesli serwer "
                                   "byl przeinstalowany, usun stary wpis (ssh-keygen -R adres) i sprobuj ponownie.",
                             "This server's key differs from the one in ~/.ssh/known_hosts. If the server was "
                             "reinstalled, remove the old entry (ssh-keygen -R address) and try again.")) from None
    except paramiko.AuthenticationException:
        raise SetupError(_tr(lang, "Serwer nie przyjal hasla albo klucza. Sprawdz uzytkownika i dane logowania.",
                             "The server rejected the password or key. Check the user and the credentials.")) from None
    except (OSError, paramiko.SSHException) as exc:
        raise SetupError(_tr(lang, f"Nie moge polaczyc sie z serwerem: {exc}",
                             f"Cannot connect to the server: {exc}")) from None
    try:
        progress(_tr(lang, "Polaczono.", "Connected."))
        identity = key_file
        if password:
            # Haslo tylko na te sesje — dalej Pipe loguje sie wlasnym kluczem
            path, public = _own_key(host, user, port)
            code, _ = _run(client, "umask 077; mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && "
                                   f"(grep -qxF {shlex.quote(public)} ~/.ssh/authorized_keys || "
                                   f"printf '%s\\n' {shlex.quote(public)} >> ~/.ssh/authorized_keys)")
            if code != 0:
                raise SetupError(_tr(lang, "Nie udalo sie dodac klucza Pipe do ~/.ssh/authorized_keys.",
                                     "Could not add Pipe's key to ~/.ssh/authorized_keys."))
            identity = str(path)
            progress(_tr(lang, f"Dodano klucz Pipe ({path.name}) — kolejne polaczenia bez hasla.",
                         f"Added Pipe's key ({path.name}) — next connections without a password."))
        _, uid = _run(client, "id -u")
        script = shlex.quote(remote_script(lang))
        if uid.strip() == "0":
            command, stdin = f"bash -c {script}", ""
        elif password:
            command, stdin = f"sudo -S -p '' bash -c {script}", password + "\n"
        else:
            command, stdin = f"sudo -n bash -c {script}", ""
        progress(_tr(lang, "Instaluje Pipe na serwerze (Docker, obraz, konfiguracja) — to potrwa kilka minut…",
                     "Installing Pipe on the server (Docker, image, configuration) — this takes a few minutes…"))
        code, output = _run(client, command, stdin=stdin, on_line=progress, timeout=REMOTE_TIMEOUT, secret=password)
        token = next((line[len(TOKEN_MARK):].strip() for line in output.splitlines() if line.startswith(TOKEN_MARK)), "")
        if code != 0 or TOKEN_MARK not in output:
            if "password is required" in output or "a terminal is required" in output:
                raise SetupError(_tr(lang, "Uzytkownik nie jest rootem, a sudo wymaga hasla — podaj haslo.",
                                     "The user is not root and sudo needs a password — enter the password."))
            raise SetupError(_tr(lang, f"Instalacja na serwerze nie powiodla sie (kod {code}) — szczegoly wyzej.",
                                 f"The installation on the server failed (code {code}) — details above."))
    finally:
        client.close()
    progress(_tr(lang, "Pipe dziala na serwerze.", "Pipe runs on the server."))
    return {"kind": "server", "host": host, "user": user, "ssh_port": port, "key": identity, "token": token}
