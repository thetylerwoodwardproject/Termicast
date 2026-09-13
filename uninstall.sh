#!/usr/bin/env bash
# Termicast uninstaller
#
# One-command uninstall:
#   curl -fsSL https://raw.githubusercontent.com/thetylerwoodwardproject/Termicast/main/uninstall.sh | bash
#
# Removes the app from /opt/termicast and the `termicast` link on your PATH.
# Saved state (podcasts, episodes, database) lives in ~/.local/share/termicast
# and is kept by default; pass --purge to delete it too.
set -euo pipefail

INSTALL_DIR="/opt/termicast"
BIN_LINK="$HOME/.local/bin/termicast"
STATE_DIR="$HOME/.local/share/termicast"

PURGE=0
if [ "${1:-}" = "--purge" ]; then
  PURGE=1
fi

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

# 1. Remove the install directory (the app and its virtual environment)
if [ -d "$INSTALL_DIR" ]; then
  step "Removing $INSTALL_DIR"
  $SUDO rm -rf "$INSTALL_DIR"
else
  ok "$INSTALL_DIR already absent"
fi

# 2. Remove the PATH link
if [ -L "$BIN_LINK" ] || [ -e "$BIN_LINK" ]; then
  step "Removing $BIN_LINK"
  rm -f "$BIN_LINK"
else
  ok "$BIN_LINK already absent"
fi

# 3. Optionally remove saved state
if [ "$PURGE" -eq 1 ]; then
  if [ -d "$STATE_DIR" ]; then
    step "Removing saved state $STATE_DIR"
    rm -rf "$STATE_DIR"
  else
    ok "$STATE_DIR already absent"
  fi
else
  warn "Kept saved state at $STATE_DIR (podcasts, episodes, database)"
  warn "Re-run with --purge to delete it too"
fi

ok "Termicast uninstalled!"
if [ "$PURGE" -ne 1 ]; then
  printf "\nYour data is still at %s.\n" "$STATE_DIR"
fi
