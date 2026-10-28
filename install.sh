#!/usr/bin/env bash
# install.sh — Instaluje skrót "pipe" w bash/zsh
# Uruchom raz: bash install.sh (po polsku: PIPE_LANG=pl bash install.sh)
# Potem wystarczy wpisać: pipe

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI_PATH="$SCRIPT_DIR/clients/cli/cli.py"

# ─── Kolory ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

t() { case "${PIPE_LANG:-en}" in pl*|PL*) printf '%s' "$1" ;; *) printf '%s' "$2" ;; esac; }  # t "polski" "english"

# ─── Zapytaj o adres serwera ─────────────────────────────────────────────────
# Pusty adres = bez serwera: `pipe web` zapyta, co chcesz robic, i postawi Pipe sam (lokalnie albo na serwerze).
VPS_HOST="${1:-}"
if [ -z "$VPS_HOST" ]; then
    printf "${CYAN}%s ${NC}" "$(t 'Podaj adres serwera (np. root@serwer.example.com) albo Enter, zeby skonfigurowac w aplikacji:' 'Server address (e.g. root@server.example.com), or Enter to set it up in the app:')"
    read -r VPS_HOST
fi

SSH_PORT="${2:-22}"
SSH_KEY="${3:-}"

# ─── Sprawdź Python ──────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null && ! command -v python &>/dev/null; then
    echo -e "${RED}❌ $(t 'Błąd: Python3 nie jest zainstalowany.' 'Error: Python3 is not installed.')${NC}"
    exit 1
fi

PYTHON=$(command -v python3 || command -v python)

# ─── Zainstaluj zależności ───────────────────────────────────────────────────
echo -e "${CYAN}📦 $(t 'Instaluję zależności CLI...' 'Installing the CLI dependencies...')${NC}"
$PYTHON -m pip install -r "$SCRIPT_DIR/clients/cli/requirements.txt" --quiet

# ─── Buduj alias ─────────────────────────────────────────────────────────────
PIPE_ARGS=""
if [ -n "$VPS_HOST" ]; then
    PIPE_ARGS="--host \"$VPS_HOST\""
fi
if [ -n "$VPS_HOST" ] && [ "$SSH_PORT" != "22" ]; then
    PIPE_ARGS="$PIPE_ARGS --ssh-port $SSH_PORT"
fi
if [ -n "$SSH_KEY" ]; then
    PIPE_ARGS="$PIPE_ARGS --key \"$SSH_KEY\""
fi

ALIAS_BLOCK="
# ─── VPS Management Agent (Pipe) ───────────────────────────────────────────
pipe() {
    $PYTHON \"$CLI_PATH\" $PIPE_ARGS \"\$@\"
}
# ─── end Pipe ───────────────────────────────────────────────────────────────"

# ─── Wykryj powłokę i wybierz plik profilu ───────────────────────────────────
if [ -n "$ZSH_VERSION" ] || [ "$SHELL" = "$(which zsh 2>/dev/null)" ]; then
    RC_FILE="$HOME/.zshrc"
elif [ -n "$BASH_VERSION" ] || [ "$SHELL" = "$(which bash 2>/dev/null)" ]; then
    RC_FILE="$HOME/.bashrc"
else
    RC_FILE="$HOME/.profile"
fi

echo -e "${CYAN}📝 $(t "Dodaję skrót 'pipe' do" "Adding the 'pipe' shortcut to") $RC_FILE...${NC}"

# Usuń poprzedni blok (jeśli istnieje)
# Przedrostek "(Pipe" lapie tez blok ze starsza nazwa komendy — ponowna
# instalacja zastepuje go, zamiast zostawiac dwie funkcje w profilu.
if grep -q "VPS Management Agent (Pipe" "$RC_FILE" 2>/dev/null; then
    # Usuń stary blok między znacznikami
    sed -i.bak '/# ─── VPS Management Agent (Pipe/,/# ─── end Pipe/d' "$RC_FILE"
    echo -e "${YELLOW}🔄 $(t "Zaktualizowano istniejący skrót 'pipe'." "Updated the existing 'pipe' shortcut.")${NC}"
fi

# Dodaj nowy blok
printf '%s\n' "$ALIAS_BLOCK" >> "$RC_FILE"

echo ""
echo -e "${CYAN}════════════════════════════════════════════════════${NC}"
if [ -n "$VPS_HOST" ]; then
    echo -e "${CYAN} $(t "Gotowe! Załaduj profil i wpisz 'pipe' aby połączyć się z:" "Done! Reload your profile and type 'pipe' to connect to:")${NC}"
    echo -e "${NC}   $VPS_HOST${NC}"
else
    echo -e "${CYAN} $(t "Gotowe! Załaduj profil i wpisz 'pipe web' — strona zapyta, co chcesz robić," "Done! Reload your profile and type 'pipe web': the page asks what you want to do")${NC}"
    echo -e "${CYAN} $(t 'i postawi Pipe na tym komputerze albo na Twoim serwerze.' 'and sets Pipe up on this computer or on your server.')${NC}"
fi
echo ""
echo -e "${CYAN} $(t 'Aby zastosować teraz (w bieżącym terminalu):' 'To use it now (in this terminal):')${NC}"
echo -e "${YELLOW}   source $RC_FILE${NC}"
echo -e "${CYAN} $(t 'W nowym terminalu wystarczy wpisać:' 'In a new terminal just type:')${NC}"
echo -e "${YELLOW}   pipe${NC}"
echo -e "${CYAN}════════════════════════════════════════════════════${NC}"
