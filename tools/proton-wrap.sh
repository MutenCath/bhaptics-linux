#!/usr/bin/env bash
# Steam launch options wrapper: starts the game, then runs the
# BhapticsPlayer.exe stub inside the same Proton session so
# bhaptics_library's isPlayerRunning() check passes.
#
# Usage (Steam launch options):  /path/to/proton-wrap.sh %command%
"$@" &
game=$!

proton=""
for a in "$@"; do
  case "$a" in */proton) proton="$a" ;; esac
done
if [ -n "$proton" ]; then
  ( sleep 15; "$proton" run 'C:\BhapticsPlayer.exe' /k rem >/dev/null 2>&1 ) &
fi

wait "$game"
