#!/usr/bin/env bash
# Termicast installer
#
# One-command install:
#   curl -fsSL https://raw.githubusercontent.com/thetylerwoodwardproject/Termicast/main/install.sh | bash
#
# Runs as your normal user and uses `sudo` internally only for the privileged
# steps (installing packages and preparing /opt/termicast). Safe to re-run to
# pick up updates.
set -euo pipefail

REPO="https://github.com/thetylerwoodwardproject/Termicast.git"
INSTALL_DIR="/opt/termicast"

C=$(tput setaf 6 2>/dev/null || true)
G=$(tput setaf 2 2>/dev/null || true)
Y=$(tput setaf 3 2>/dev/null || true)
R=$(tput setaf 1 2>/dev/null || true)
N=$(tput sgr0 2>/dev/null || true)
step() { printf "%b[termicast]%b %s\n" "$C" "$N" "$1"; }
ok()   { printf "%b[termicast]%b %s\n" "$G" "$N" "$1"; }
warn() { printf "%b[termicast]%b %s\n" "$Y" "$N" "$1"; }
die()  { printf "%b[termicast]%b %s\n" "$R" "$N" "$1" >&2; exit 1; }

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || die "Need root access for $INSTALL_DIR. Run as root or install sudo."
  SUDO="sudo"
fi

# 1. System packages (only what's missing)
need=()
command -v git    >/dev/null 2>&1 || need+=(git)
command -v ffmpeg >/dev/null 2>&1 || need+=(ffmpeg)
if ! command -v python3 >/dev/null 2>&1 || \
   ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
  need+=(python3 python3-venv)
fi
if [ ${#need[@]} -gt 0 ]; then
  step "Installing system packages: ${need[*]}"
  $SUDO apt-get update -y
  $SUDO apt-get install -y "${need[@]}"
else
  ok "System dependencies already present"
fi

# 2. Prepare the install directory and get the code
step "Preparing $INSTALL_DIR"
if [ -d "$INSTALL_DIR/.git" ]; then
  $SUDO chown -R "$(id -u):$(id -g)" "$INSTALL_DIR"
  git -C "$INSTALL_DIR" pull --ff-only
elif [ -d "$INSTALL_DIR" ] && [ -n "$(ls -A "$INSTALL_DIR")" ]; then
  die "$INSTALL_DIR exists but isn't a Termicast checkout. Move it aside and re-run."
else
  $SUDO mkdir -p "$INSTALL_DIR"
  $SUDO chown -R "$(id -u):$(id -g)" "$INSTALL_DIR"
  git clone "$REPO" "$INSTALL_DIR"
fi

# 3. Create the venv and install
cd "$INSTALL_DIR"
step "Creating Python virtual environment"
python3 -m venv .venv
step "Installing Termicast (editable, with dev dependencies)"
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'

# 4. Put termicast on PATH
step "Linking termicast onto your PATH"
mkdir -p "$HOME/.local/bin"
ln -sf "$INSTALL_DIR/.venv/bin/termicast" "$HOME/.local/bin/termicast"

ok "Termicast installed successfully!"
printf "\nRun it with:  termicast\n"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) warn "Add $HOME/.local/bin to your PATH (log out and back in)." ;;
esac
