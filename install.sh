#!/usr/bin/env bash
# Sets up (or repairs after moving the folder) the venv and systemd user service.
set -euo pipefail
cd "$(dirname "$(realpath "$0")")"

if ! ./venv/bin/python -c 'import sys' >/dev/null 2>&1; then
  echo "creating venv..."
  rm -rf venv
  python3 -m venv venv
fi
./venv/bin/pip install -q -r requirements.txt
# optional tray deps; the tray still needs system PyGObject (python-gobject)
./venv/bin/pip install -q pystray >/dev/null 2>&1 || echo "note: pystray install failed — tray unavailable"

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

systemctl --user daemon-reload
systemctl --user enable bhaptics-daemon.service >/dev/null 2>&1
systemctl --user restart bhaptics-daemon.service

chmod +x vestctl bhaptics-tray
mkdir -p "$HOME/.local/bin"
ln -sf "$PWD/vestctl" "$HOME/.local/bin/vestctl"
ln -sf "$PWD/bhaptics-tray" "$HOME/.local/bin/bhaptics-tray"

mkdir -p "$HOME/.local/share/applications"
cp packaging/bhaptics-vest.desktop "$HOME/.local/share/applications/"
cp packaging/bhaptics-tray.desktop "$HOME/.local/share/applications/"

if command -v fish >/dev/null 2>&1; then
  mkdir -p "$HOME/.config/fish/completions"
  cp packaging/vestctl.fish "$HOME/.config/fish/completions/"
fi

echo "installed & started from $PWD"
echo "UI: http://127.0.0.1:15881/ui — CLI: vestctl"
echo "tray: bhaptics-tray (uses system python-gobject for the tray icon)"
