#!/usr/bin/env bash
# Sets up (or repairs after moving the folder) the venv and systemd user service.
set -euo pipefail
cd "$(dirname "$(realpath "$0")")"

uid="$(id -u)"

die() { echo "error: $*" >&2; exit 1; }

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

# --- python -------------------------------------------------------------

command -v python3 >/dev/null 2>&1 || die "python3 not found.
Install Python 3.10 or newer:
  Debian/Ubuntu: sudo apt install python3 python3-venv python3-pip
  Fedora:        sudo dnf install python3
  Arch/SteamOS:  sudo pacman -S python  (read-only rootfs: sudo steamos-readonly disable first)"

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "python3 is $(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))') but 3.10+ is required."

# --- venv + pip ---------------------------------------------------------

# A venv counts as healthy only if pip works inside it — an interrupted earlier
# run (or a distro without ensurepip) leaves a pip-less venv that used to pass
# the old check and then fail on every re-run.
venv_ok() {
  [ -x venv/bin/python ] && venv/bin/python -m pip --version >/dev/null 2>&1
}

bootstrap_pip() {
  # ensurepip works out of the box on Arch/Fedora; Debian/Ubuntu strip it and
  # tell you to apt-install python3-pip instead, which is not an option on a
  # read-only rootfs (SteamOS) — so fall back to the official get-pip.py.
  if venv/bin/python -m ensurepip --upgrade 2>/dev/null; then
    return 0
  fi
  echo "ensurepip unavailable; bootstrapping pip via get-pip.py..."
  local getpip
  getpip="$(mktemp)"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$getpip"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$getpip" https://bootstrap.pypa.io/get-pip.py
  else
    rm -f "$getpip"
    die "neither curl nor wget available to fetch get-pip.py.
Install pip for your distro instead:
  Debian/Ubuntu: sudo apt install python3-pip
  Fedora:        sudo dnf install python3-pip
  Arch/SteamOS:  sudo pacman -S python-pip"
  fi
  venv/bin/python "$getpip" || { rm -f "$getpip"; die "get-pip.py failed"; }
  rm -f "$getpip"
}

if ! venv_ok; then
  echo "creating venv..."
  rm -rf venv
  if python3 -m venv venv 2>/dev/null && venv/bin/python -m pip --version >/dev/null 2>&1; then
    : # fast path: distro python ships a working ensurepip
  else
    rm -rf venv
    python3 -m venv --without-pip venv 2>/dev/null \
      || die "the python3 venv module is missing or broken.
Install it for your distro:
  Debian/Ubuntu: sudo apt install python3-venv python3-pip
  Fedora:        sudo dnf install python3-pip
  Arch/SteamOS:  sudo pacman -S python-pip  (read-only rootfs: sudo steamos-readonly disable first)"
    bootstrap_pip
  fi
  venv_ok || die "pip is still unavailable inside the venv — see the messages above."
fi

# --- dependencies -------------------------------------------------------

./venv/bin/pip install -q --upgrade pip || true

# Core deps come from requirements.txt minus the optional entries. evdev is
# source-only on PyPI (no wheels for any platform), so it needs gcc + Python
# headers at install time — absent on minimal or read-only systems — and the
# daemon already runs fine without it (the gamepad rumble mirror stays off).
core="$(grep -viE '^(evdev|pystray)([<>= ]|$)' requirements.txt)" \
  || die "requirements.txt is missing or empty — re-clone the repo."
# shellcheck disable=SC2086 # intentional word splitting: one package per word
./venv/bin/pip install -q $core
./venv/bin/pip install -q evdev 2>/dev/null \
  || echo "note: evdev unavailable — gamepad rumble mirror disabled (it builds from source and needs gcc + Python headers: base-devel / python3-dev)"
# optional tray deps; the tray still needs system PyGObject (python-gobject)
./venv/bin/pip install -q pystray >/dev/null 2>&1 || echo "note: pystray install failed — tray unavailable"

# --- preflight (warnings only, nothing here blocks the install) ----------

warnings=()
if ! systemctl is-active --quiet bluetooth 2>/dev/null && ! command -v bluetoothctl >/dev/null 2>&1; then
  warnings+=("BlueZ (bluetoothd) not detected — the vest link needs working Bluetooth")
fi
if ! command -v pactl >/dev/null 2>&1 || ! command -v parec >/dev/null 2>&1; then
  warnings+=("pactl/parec not found — audio-to-haptics needs PipeWire's pulse tools (pipewire-pulse / libpulse)")
fi
if ! command -v openssl >/dev/null 2>&1; then
  warnings+=("openssl not found — needed once to mint the SDK2 TLS cert for newer games")
fi
if [ "${#warnings[@]}" -gt 0 ]; then
  echo
  echo "heads-up: some optional/runtime pieces look missing:"
  for w in "${warnings[@]}"; do echo "  - $w"; done
  echo
fi

# --- systemd user service ------------------------------------------------

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

# Enable + (re)start the user service last. If the session has no usable user
# bus, the files above are still installed and the user gets a clear next step
# rather than an opaque systemctl error aborting the script halfway through.
if systemctl --user daemon-reload \
  && systemctl --user enable bhaptics-daemon.service >/dev/null \
  && systemctl --user restart bhaptics-daemon.service; then
  echo "installed & started from $PWD"
  echo "UI: http://127.0.0.1:15881/ui — CLI: vestctl"
  echo "tray: bhaptics-tray (uses system python-gobject for the tray icon)"
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
