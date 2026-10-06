#!/usr/bin/env bash
# Sets up (or repairs after moving the folder) the venv and systemd user service.
set -euo pipefail
cd "$(dirname "$(realpath "$0")")"

uid="$(id -u)"

# systemd --user talks to the session bus under $XDG_RUNTIME_DIR. Some sessions
# point that variable at a subdirectory (notably SteamOS Desktop Mode's nested
# Plasma, where it can be /run/user/<uid>/nested_plasma), which makes systemctl
# fail with "Failed to connect to user scope bus via local transport: No such
# file or directory". The canonical per-user bus lives at /run/user/<uid>/bus,
# so prefer it whenever it exists. These exports only affect this script and the
# commands it runs; the caller's shell is untouched.
if [ -S "/run/user/$uid/bus" ]; then
  export XDG_RUNTIME_DIR="/run/user/$uid"
  export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus"
fi

if ! ./venv/bin/python -c 'import sys' >/dev/null 2>&1; then
  echo "creating venv..."
  rm -rf venv
  python3 -m venv venv
fi
./venv/bin/pip install -q -r requirements.txt

mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/bhaptics-daemon.service" <<EOF
[Unit]
Description=bHaptics Player emulator (WebSocket -> BLE TactSuit)
After=bluetooth.target

[Service]
ExecStart=$PWD/venv/bin/python $PWD/player_daemon.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

chmod +x vestctl
mkdir -p "$HOME/.local/bin"
ln -sf "$PWD/vestctl" "$HOME/.local/bin/vestctl"

mkdir -p "$HOME/.local/share/applications"
cp packaging/bhaptics-vest.desktop "$HOME/.local/share/applications/"

if command -v fish >/dev/null 2>&1; then
  mkdir -p "$HOME/.config/fish/completions"
  cp packaging/vestctl.fish "$HOME/.config/fish/completions/"
fi

# Enable + (re)start the user service last. If the session has no usable user
# bus, the files above are still installed and the user gets a clear next step
# rather than an opaque systemctl error aborting the script halfway through.
if systemctl --user daemon-reload \
  && systemctl --user enable bhaptics-daemon.service >/dev/null \
  && systemctl --user restart bhaptics-daemon.service; then
  echo "installed & started from $PWD"
  echo "UI: http://127.0.0.1:15881/ui — CLI: vestctl"
else
  cat >&2 <<EOF
installed, but the systemd user service could not be started.

Run this installer inside your graphical login session as your normal user —
not over SSH, and never with sudo. Start the service from a Desktop Mode
terminal with:

  export XDG_RUNTIME_DIR=/run/user/$uid
  export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid/bus
  systemctl --user daemon-reload
  systemctl --user enable --now bhaptics-daemon.service
EOF
  exit 1
fi
