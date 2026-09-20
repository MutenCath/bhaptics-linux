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

chmod +x vestctl
mkdir -p "$HOME/.local/bin"
ln -sf "$PWD/vestctl" "$HOME/.local/bin/vestctl"

mkdir -p "$HOME/.local/share/applications"
cp packaging/bhaptics-vest.desktop "$HOME/.local/share/applications/"

if command -v fish >/dev/null 2>&1; then
  mkdir -p "$HOME/.config/fish/completions"
  cp packaging/vestctl.fish "$HOME/.config/fish/completions/"
fi

echo "installed & started from $PWD"
echo "UI: http://127.0.0.1:15881/ui — CLI: vestctl"
