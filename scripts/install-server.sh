#!/usr/bin/env bash
# Instalacja backendu Pipe na serwerze (Docker albo natywnie z systemd).
#
#   sudo bash scripts/install-server.sh                  # z repozytorium, tryb docker, kreator pyta o providera
#   sudo bash scripts/install-server.sh --mode native    # bez Dockera: venv + systemd
#
# Bez pytan (cloud-init, Ansible, CI) — konfiguracja ze zmiennych srodowiska:
#   sudo LLM_PROVIDER=gemini LLM_API_KEY=... bash scripts/install-server.sh --non-interactive
#
# Jezyk agenta (prompty, komunikaty, alerty): --lang pl|en albo zmienna PIPE_LANG; kreator tez o niego pyta.
# English: sudo bash scripts/install-server.sh --lang en
#
# Na swiezym serwerze bez repozytorium skrypt sam je sklonuje (--repo, --dir).
# Kubernetes: patrz deploy/kubernetes/README.md (kubectl apply -k).
set -euo pipefail

MODE="docker"
DIR="/opt/pipe"
REPO="${PIPE_REPO:-https://github.com/Kvmyk/pipe.git}"
BRANCH="${PIPE_BRANCH:-main}"
NON_INTERACTIVE=0

usage() {
  sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
  echo
  echo "Opcje: --mode docker|native  --dir KATALOG  --repo URL  --branch GALAZ  --lang pl|en  --non-interactive"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE="${2:?}"; shift 2 ;;
    --dir) DIR="${2:?}"; shift 2 ;;
    --repo) REPO="${2:?}"; shift 2 ;;
    --branch) BRANCH="${2:?}"; shift 2 ;;
    --lang) case "${2:?}" in pl|en) PIPE_LANG="$2"; export PIPE_LANG ;; *) echo "--lang: pl | en" >&2; exit 2 ;; esac; shift 2 ;;
    --non-interactive|-y) NON_INTERACTIVE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Nieznana opcja / unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$MODE" in docker|native) ;; *) echo "--mode: docker | native" >&2; exit 2 ;; esac

t() { case "${PIPE_LANG:-pl}" in en*|EN*) printf '%s' "$2" ;; *) printf '%s' "$1" ;; esac; }  # t "polski" "english"
log() { printf '\033[36m[pipe]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[pipe] %s\033[0m %s\n' "$(t 'BLAD:' 'ERROR:')" "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "$(t 'uruchom jako root (sudo) — Pipe zarzadza calym serwerem.' 'run as root (sudo) — Pipe manages the whole server.')"

# ─── Menedzer pakietow ───────────────────────────────────────────────────────
if command -v apt-get >/dev/null 2>&1; then PM=apt
elif command -v dnf >/dev/null 2>&1; then PM=dnf
elif command -v yum >/dev/null 2>&1; then PM=yum
elif command -v apk >/dev/null 2>&1; then PM=apk
elif command -v pacman >/dev/null 2>&1; then PM=pacman
elif command -v zypper >/dev/null 2>&1; then PM=zypper
else PM=none
fi

pkg_install() {
  case "$PM" in
    apt) DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" ;;
    dnf) dnf install -y -q "$@" ;;
    yum) yum install -y -q "$@" ;;
    apk) apk add --no-cache "$@" ;;
    pacman) pacman -Sy --noconfirm --needed "$@" ;;
    zypper) zypper --non-interactive install "$@" ;;
    *) die "$(t 'nie rozpoznano menedzera pakietow — zainstaluj recznie:' 'package manager not recognised — install manually:') $*" ;;
  esac
}

need() {  # need <komenda> <pakiet...>
  local cmd="$1"; shift
  command -v "$cmd" >/dev/null 2>&1 || { log "$(t Instaluje Installing) $*"; pkg_install "$@"; }
}

need git git
need python3 python3

# ─── Kod ────────────────────────────────────────────────────────────────────
# Uruchomiony z repozytorium — uzyj go; inaczej sklonuj do $DIR.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/../backend/server.py" ]; then
  DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
  log "$(t Repozytorium Repository): $DIR"
elif [ -d "$DIR/.git" ]; then
  log "$(t Aktualizuje Updating) $DIR"
  git -C "$DIR" pull --ff-only
else
  log "$(t Klonuje Cloning) $REPO ($BRANCH) -> $DIR"
  git clone --branch "$BRANCH" --depth 1 "$REPO" "$DIR"
fi
cd "$DIR"

# ─── Konfiguracja LLM ───────────────────────────────────────────────────────
if [ "$NON_INTERACTIVE" -eq 1 ]; then
  python3 -m backend.configure --from-env
elif [ ! -f backend/.env ] || ! grep -qE '^LLM_(PROVIDER|BASE_URL)=.+' backend/.env; then
  python3 -m backend.configure
else
  log "$(t 'backend/.env juz istnieje — pomijam kreator (zmiana: python3 -m backend.configure)' 'backend/.env already exists — skipping the wizard (to change: python3 -m backend.configure)')"
  # Kreator pominiety — jezyk podany jawnie (--lang / PIPE_LANG) i tak trafia do backend/.env.
  if [ -n "${PIPE_LANG:-}" ]; then
    if grep -q '^PIPE_LANG=' backend/.env; then
      sed -i "s#^PIPE_LANG=.*#PIPE_LANG=$PIPE_LANG#" backend/.env
    else
      printf 'PIPE_LANG=%s\n' "$PIPE_LANG" >> backend/.env
    fi
  fi
fi
# Od tej chwili jezykiem instalatora jest ten zapisany w backend/.env (np. wybrany w kreatorze).
if [ -f backend/.env ]; then
  SAVED_LANG="$(sed -n 's/^PIPE_LANG=//p' backend/.env | head -n1)"
  if [ -n "$SAVED_LANG" ]; then PIPE_LANG="$SAVED_LANG"; export PIPE_LANG; fi
fi
mkdir -p backend/data

# Zsynchronizuj AGENT_TOKEN (inaczej bot sie nie polaczy) i PIPE_LANG (ten sam jezyk) do .env bota Telegram.
sync_to_telegram() {  # sync_to_telegram KLUCZ
  local key="$1" value
  value="$(sed -n "s/^$key=//p" backend/.env | head -n1)"
  [ -n "$value" ] || return 0
  if grep -q "^$key=" clients/telegram/.env; then
    grep -q "^$key=$value\$" clients/telegram/.env || \
      sed -i "s#^$key=.*#$key=$value#" clients/telegram/.env
  else
    printf '%s=%s\n' "$key" "$value" >> clients/telegram/.env
  fi
}
if [ -f backend/.env ] && [ -f clients/telegram/.env ]; then
  sync_to_telegram AGENT_TOKEN
  sync_to_telegram PIPE_LANG
fi

# ─── Docker ─────────────────────────────────────────────────────────────────
compose() {
  if docker compose version >/dev/null 2>&1; then docker compose "$@"
  elif command -v docker-compose >/dev/null 2>&1; then docker-compose "$@"
  else die "$(t 'brak docker compose' 'docker compose not found')"; fi
}

install_docker() {
  command -v docker >/dev/null 2>&1 && return
  log "$(t 'Instaluje Dockera z repozytorium dystrybucji' 'Installing Docker from the distribution repository')"
  case "$PM" in
    apt) pkg_install docker.io; pkg_install docker-compose-v2 2>/dev/null || pkg_install docker-compose ;;
    dnf|yum) pkg_install docker docker-compose-plugin 2>/dev/null || pkg_install moby-engine docker-compose ;;
    apk) pkg_install docker docker-cli-compose ;;
    pacman) pkg_install docker docker-compose ;;
    zypper) pkg_install docker docker-compose ;;
    *) die "$(t 'zainstaluj Dockera recznie:' 'install Docker manually:') https://docs.docker.com/engine/install/" ;;
  esac
  systemctl enable --now docker 2>/dev/null || rc-update add docker default 2>/dev/null || true
  service docker start 2>/dev/null || true
}

if [ "$MODE" = "docker" ]; then
  install_docker
  cd backend
  if [ -f ../clients/telegram/.env ]; then
    log "$(t 'Buduje i uruchamiam backend + bota Telegram' 'Building and starting the backend + Telegram bot')"
    compose up -d --build
  else
    log "$(t 'Buduje i uruchamiam backend (bot Telegram: utworz clients/telegram/.env i powtorz)' 'Building and starting the backend (Telegram bot: create clients/telegram/.env and run again)')"
    compose up -d --build vps-agent
  fi
  cd ..
fi

# ─── Native (systemd) ───────────────────────────────────────────────────────
if [ "$MODE" = "native" ]; then
  command -v systemctl >/dev/null 2>&1 || die "$(t 'tryb native wymaga systemd' 'native mode needs systemd')"
  # `venv --help` dziala tez bez ensurepip — sprawdzamy to, czego venv naprawde potrzebuje
  if ! python3 -c "import ensurepip, venv" >/dev/null 2>&1; then
    case "$PM" in
      apt) pkg_install python3-venv ;;
      dnf|yum) pkg_install python3-pip ;;
      apk) pkg_install py3-virtualenv py3-pip ;;
      *) die "$(t 'brak modulu venv/ensurepip — zainstaluj pakiet python3-venv' 'venv/ensurepip module missing — install the python3-venv package')" ;;
    esac
  fi
  log "$(t 'Tworze srodowisko Pythona w' 'Creating the Python environment in') $DIR/.venv"
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r backend/requirements.txt
  if [ -f clients/telegram/.env ]; then .venv/bin/pip install -q -r clients/telegram/requirements.txt; fi

  # W trybie native backend slucha tylko na localhost (dostep: tunel SSH), a sciezki
  # kontenerowe (/app/...) z .env.example zamieniamy na katalog instalacji.
  env_set() {  # env_set KLUCZ WARTOSC — ustawia, gdy brak albo gdy wskazuje na /app/
    if grep -qE "^$1=(/app/.*)?$" backend/.env; then
      sed -i "s#^$1=.*#$1=$2#" backend/.env
    elif ! grep -q "^$1=" backend/.env; then
      echo "$1=$2" >> backend/.env
    fi
  }
  env_set TCP_HOST 127.0.0.1
  env_set PIPE_RUNTIME native
  env_set DATA_DIR "$DIR/backend/data"
  env_set AUDIT_LOG_PATH "$DIR/backend/data/audit.log"

  for unit in pipe pipe-telegram; do
    if [ "$unit" = pipe-telegram ] && [ ! -f clients/telegram/.env ]; then continue; fi
    sed "s#@PIPE_DIR@#$DIR#g" "deploy/systemd/$unit.service" > "/etc/systemd/system/$unit.service"
  done
  systemctl daemon-reload
  systemctl enable --now pipe
  if [ -f clients/telegram/.env ]; then systemctl enable --now pipe-telegram; fi
fi

# ─── Podsumowanie ───────────────────────────────────────────────────────────
sleep 3
if (exec 3<>/dev/tcp/127.0.0.1/7379) 2>/dev/null; then
  log "$(t 'Backend odpowiada na' 'The backend responds on') 127.0.0.1:7379"
else
  log "$(t 'Backend jeszcze nie odpowiada — logi:' 'The backend does not respond yet — logs:') $( [ "$MODE" = docker ] && echo 'cd backend && docker compose logs -f vps-agent' || echo 'journalctl -u pipe -f')"
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
case "${PIPE_LANG:-pl}" in
  en*|EN*) cat <<EOF

Done. On your computer:
  bash install.sh            # Linux/macOS (or .\\install.ps1 on Windows)
  pipe --lang en --host root@${IP:-YOUR_SERVER}

Telegram: fill in clients/telegram/.env (TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_IDS)
and run the script again.
EOF
  ;;
  *) cat <<EOF

Gotowe. Na swoim komputerze:
  bash install.sh            # Linux/macOS (albo .\\install.ps1 na Windows)
  pipe --host root@${IP:-TWOJ_SERWER}

Telegram: uzupelnij clients/telegram/.env (TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_IDS)
i uruchom skrypt ponownie.
EOF
  ;;
esac
