#!/usr/bin/env bash
# Prepares a Proton/Wine prefix for bHaptics SDK2 games.
#
# bhaptics_library.dll refuses to connect unless it thinks the Windows
# bHaptics Player is installed (registry class bhaptics-app) and running
# (a process named BhapticsPlayer.exe). This script fakes the registry key
# and drops a stub exe; use proton-wrap.sh in Steam launch options to have
# the stub running during the game.
#
# Usage:
#   ./proton-sdk2-setup.sh <steam-appid | path-to-compatdata-pfx>
set -euo pipefail

arg="${1:?usage: $0 <steam-appid | path-to-pfx>}"

if [[ "$arg" =~ ^[0-9]+$ ]]; then
  pfx=""
  # every Steam library is listed in libraryfolders.vdf; check them all
  for steamroot in "$HOME/.steam/steam" "$HOME/.local/share/Steam" \
                   "$HOME/.var/app/com.valvesoftware.Steam/data/Steam"; do
    vdf="$steamroot/steamapps/libraryfolders.vdf"
    [ -f "$vdf" ] || continue
    while IFS= read -r lib; do
      cand="$lib/steamapps/compatdata/$arg/pfx"
      [ -d "$cand" ] && pfx="$cand" && break 2
    done < <(grep -oP '"path"\s+"\K[^"]+' "$vdf")
  done
  [ -n "$pfx" ] || { echo "compatdata for appid $arg not found — run the game once first"; exit 1; }
else
  pfx="$arg"
fi
[ -f "$pfx/user.reg" ] || { echo "$pfx does not look like a Wine prefix (no user.reg)"; exit 1; }
echo "prefix: $pfx"

if ! grep -q 'bhaptics-app' "$pfx/user.reg"; then
  cat >> "$pfx/user.reg" <<EOF

[Software\\\\Classes\\\\bhaptics-app] $(date +%s)
@="bHaptics Player"

[Software\\\\Classes\\\\bhaptics-app\\\\shell\\\\open\\\\command] $(date +%s)
@="\\"C:\\\\\\\\BhapticsPlayer.exe\\" \\"%1\\""
EOF
  echo "registry: bhaptics-app class added"
else
  echo "registry: bhaptics-app already present"
fi

stub="$pfx/drive_c/BhapticsPlayer.exe"
if [ ! -f "$stub" ]; then
  cp "$pfx/drive_c/windows/system32/cmd.exe" "$stub"
  echo "stub: created drive_c/BhapticsPlayer.exe (cmd.exe copy)"
else
  echo "stub: already present"
fi

echo
echo "Done. In Steam, set the game's launch options to:"
echo "  $(realpath "$(dirname "$0")")/proton-wrap.sh %command%"
