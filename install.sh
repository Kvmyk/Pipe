#!/usr/bin/env bash
# install.sh — Instaluje skrót "pipe" w bash/zsh
# Uruchom raz: bash install.sh
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

# ─── Zapytaj o adres serwera ─────────────────────────────────────────────────
VPS_HOST="${1:-}"
if [ -z "$VPS_HOST" ]; then
    printf "${CYAN}Podaj adres serwera (np. root@mikrus.example.com): ${NC}"
    read -r VPS_HOST
fi

if [ -z "$VPS_HOST" ]; then
    echo -e "${RED}❌ Błąd: adres serwera jest wymagany.${NC}"
    exit 1
fi

SSH_PORT="${2:-22}"
SSH_KEY="${3:-}"

# ─── Sprawdź Python ──────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null && ! command -v python &>/dev/null; then
    echo -e "${RED}❌ Błąd: Python3 nie jest zainstalowany.${NC}"
    exit 1
fi

PYTHON=$(command -v python3 || command -v python)

# ─── Zainstaluj zależności ───────────────────────────────────────────────────
echo -e "${CYAN}📦 Instaluję zależności CLI...${NC}"
$PYTHON -m pip install -r "$SCRIPT_DIR/clients/cli/requirements.txt" --quiet

# ─── Buduj alias ─────────────────────────────────────────────────────────────
PIPE_ARGS="--host \"$VPS_HOST\""
if [ "$SSH_PORT" != "22" ]; then
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

echo -e "${CYAN}📝 Dodaję skrót 'pipe' do $RC_FILE...${NC}"

# Usuń poprzedni blok (jeśli istnieje)
# Przedrostek "(Pipe" lapie tez blok ze starsza nazwa komendy — ponowna
# instalacja zastepuje go, zamiast zostawiac dwie funkcje w profilu.
if grep -q "VPS Management Agent (Pipe" "$RC_FILE" 2>/dev/null; then
    # Usuń stary blok między znacznikami
    sed -i.bak '/# ─── VPS Management Agent (Pipe/,/# ─── end Pipe/d' "$RC_FILE"
    echo -e "${YELLOW}🔄 Zaktualizowano istniejący skrót 'pipe'.${NC}"
fi

# Dodaj nowy blok
printf '%s\n' "$ALIAS_BLOCK" >> "$RC_FILE"

echo ""
echo -e "${CYAN}════════════════════════════════════════════════════${NC}"
echo -e "${CYAN} Gotowe! Załaduj profil i wpisz 'pipe' aby połączyć się z:${NC}"
echo -e "${NC}   $VPS_HOST${NC}"
echo ""
echo -e "${CYAN} Aby zastosować teraz (w bieżącym terminalu):${NC}"
echo -e "${YELLOW}   source $RC_FILE${NC}"
echo -e "${CYAN} W nowym terminalu wystarczy wpisać:${NC}"
echo -e "${YELLOW}   pipe${NC}"
echo -e "${CYAN}════════════════════════════════════════════════════${NC}"
