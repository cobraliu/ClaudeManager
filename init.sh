#!/usr/bin/env bash
# ClaudeManager — environment initialisation
# Supports: Linux (Ubuntu/Debian) and macOS
#
# Usage:
#   bash init.sh              # full setup
#   bash init.sh --mirror     # use PyPI China mirror (pip)
#
set -euo pipefail

# ── colours ───────────────────────────────────────────────────────────────────
G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; B='\033[0;34m'; N='\033[0m'
log()  { echo -e "${G}[init]${N} $*"; }
warn() { echo -e "${Y}[warn]${N} $*"; }
err()  { echo -e "${R}[error]${N} $*"; exit 1; }
step() { echo -e "\n${B}══ $* ${N}"; }

# ── args ──────────────────────────────────────────────────────────────────────
PIP_INDEX=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mirror) PIP_INDEX="-i https://pypi.tuna.tsinghua.edu.cn/simple"; shift ;;
    -h|--help)
      echo "Usage: $0 [--mirror]"
      echo "  --mirror   Use Tsinghua PyPI mirror (faster in China)"
      exit 0 ;;
    *) err "Unknown option: $1" ;;
  esac
done

# ── detect platform ───────────────────────────────────────────────────────────
OS=$(uname -s)   # Linux | Darwin
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[[ "$OS" == "Linux" || "$OS" == "Darwin" ]] || err "Unsupported OS: $OS"
log "Platform: $OS  |  Root: $ROOT"

# ── helpers ───────────────────────────────────────────────────────────────────
have() { command -v "$1" &>/dev/null; }

pkg_install() {
    if [[ "$OS" == "Darwin" ]]; then
        brew install "$@"
    else
        sudo apt-get install -y "$@"
    fi
}

# ── 1. package manager ────────────────────────────────────────────────────────
step "Package manager"
if [[ "$OS" == "Darwin" ]]; then
    # curl ships with macOS by default — assume present for the Homebrew installer.
    if ! have brew; then
        log "Installing Homebrew..."
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        if [[ -f /opt/homebrew/bin/brew ]]; then
            eval "$(/opt/homebrew/bin/brew shellenv)"
        else
            eval "$(/usr/local/bin/brew shellenv)"
        fi
    fi
    log "Homebrew: $(brew --version | head -1)"
else
    log "Updating apt cache..."
    sudo apt-get update -qq
    # curl is needed downstream (NodeSource setup script). Slim images / fresh
    # containers sometimes lack it, so install it up front rather than failing
    # at step 6 with a confusing "curl: command not found".
    if ! have curl; then
        log "Installing curl..."
        sudo apt-get install -y curl
    fi
fi

# ── 2. git ────────────────────────────────────────────────────────────────────
step "Git"
if ! have git; then
    log "Installing git..."
    pkg_install git
fi
log "git: $(git --version)"

# ── 3. tmux ───────────────────────────────────────────────────────────────────
step "tmux"
if ! have tmux; then
    log "Installing tmux..."
    pkg_install tmux
fi
log "tmux: $(tmux -V)"

# ── 4. Python 3.11 ────────────────────────────────────────────────────────────
step "Python 3.11"
if ! have python3.11; then
    log "Installing Python 3.11..."
    if [[ "$OS" == "Darwin" ]]; then
        brew install python@3.11
        brew link --overwrite python@3.11 2>/dev/null || true
    else
        if ! apt-cache show python3.11 &>/dev/null 2>&1; then
            log "Adding deadsnakes PPA..."
            sudo apt-get install -y software-properties-common
            sudo add-apt-repository -y ppa:deadsnakes/ppa
            sudo apt-get update -qq
        fi
        sudo apt-get install -y python3.11 python3.11-venv python3.11-dev
    fi
fi
log "Python: $(python3.11 --version)"

# ── 5. Python venv (.venv) ────────────────────────────────────────────────────
step "Python venv (.venv)"
if [[ ! -d "$ROOT/.venv" ]]; then
    log "Creating .venv with Python 3.11..."
    python3.11 -m venv "$ROOT/.venv"
fi

PIP="$ROOT/.venv/bin/pip"
log "Upgrading pip..."
"$PIP" install --upgrade pip -q $PIP_INDEX

if [[ -f "$ROOT/requirements.txt" ]]; then
    log "Installing backend requirements..."
    # shellcheck disable=SC2086
    "$PIP" install -r "$ROOT/requirements.txt" -q $PIP_INDEX
fi
log "venv Python: $("$ROOT/.venv/bin/python" --version)"

# ── 6. Node.js (>=18) ─────────────────────────────────────────────────────────
step "Node.js (>=18)"
NEED_NODE=18

install_node() {
    log "Installing Node.js $NEED_NODE..."
    if [[ "$OS" == "Darwin" ]]; then
        brew install "node@${NEED_NODE}"
        brew link --overwrite "node@${NEED_NODE}" 2>/dev/null || true
    else
        curl -fsSL "https://deb.nodesource.com/setup_${NEED_NODE}.x" | sudo -E bash -
        sudo apt-get install -y nodejs
    fi
}

if ! have node; then
    install_node
else
    NODE_MAJOR=$(node -e "process.stdout.write(String(process.versions.node.split('.')[0]))")
    if [[ "$NODE_MAJOR" -lt "$NEED_NODE" ]]; then
        warn "Node $NODE_MAJOR found, need $NEED_NODE+. Upgrading..."
        install_node
    fi
fi
log "node: $(node --version)  |  npm: $(npm --version)"

# ── 7. frontend — npm install + build ─────────────────────────────────────────
step "Frontend"
cd "$ROOT/frontend"
log "npm install..."
npm install 2>&1 | tail -3
log "npm run build..."
npm run build 2>&1 | tail -4
cd "$ROOT"
log "Frontend built → frontend/dist/"

# ── 8. Claude Code CLI ────────────────────────────────────────────────────────
step "Claude Code CLI"
if ! have claude; then
    # npm's global prefix is root-owned on Linux when node was installed via
    # NodeSource / apt (default prefix = /usr). Without sudo the install fails
    # with EACCES. macOS / nvm setups put the prefix in a user-writable path,
    # so don't gratuitously elevate when not needed.
    NPM_PREFIX=$(npm config get prefix 2>/dev/null || echo "")
    if [[ -n "$NPM_PREFIX" && -w "$NPM_PREFIX" ]]; then
        log "Installing @anthropic-ai/claude-code globally..."
        npm install -g @anthropic-ai/claude-code
    else
        log "Installing @anthropic-ai/claude-code globally (sudo, prefix=$NPM_PREFIX)..."
        sudo npm install -g @anthropic-ai/claude-code
    fi
else
    log "claude: already installed ($(claude --version 2>/dev/null || echo 'ok'))"
fi

# ── 9. firejail (Linux only) ──────────────────────────────────────────────────
step "Security sandbox"
if [[ "$OS" == "Linux" ]]; then
    if ! have firejail; then
        log "Installing firejail..."
        sudo apt-get install -y firejail
    fi
    log "firejail: $(firejail --version 2>&1 | head -1)"
    log "SSH shell → firejail (OS-level sandbox)"
else
    log "macOS: SSH shell → bash init-file restrictions"
fi

# ── summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${G}╔══════════════════════════════════════╗${N}"
echo -e "${G}║   ClaudeManager — environment ready  ║${N}"
echo -e "${G}╚══════════════════════════════════════╝${N}"
printf "  %-10s %s\n" "OS:"     "$OS"
printf "  %-10s %s\n" "Python:" "$(python3.11 --version)"
printf "  %-10s %s\n" "venv:"   "$ROOT/.venv"
printf "  %-10s %s\n" "Node:"   "$(node --version)"
printf "  %-10s %s\n" "npm:"    "$(npm --version)"
printf "  %-10s %s\n" "tmux:"   "$(tmux -V)"
if have claude; then
    printf "  %-10s %s\n" "claude:" "$(claude --version 2>/dev/null || echo 'installed')"
fi
if [[ "$OS" == "Linux" ]] && have firejail; then
    printf "  %-10s %s\n" "sandbox:" "firejail — $(firejail --version 2>&1 | head -1)"
else
    printf "  %-10s %s\n" "sandbox:" "bash init-file"
fi
echo ""
echo "  To start the server:"
echo "    bash $ROOT/restart.sh [port] [--ngrok] [--domain <ngrok-domain>]"
echo ""
