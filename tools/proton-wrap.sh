#!/usr/bin/env bash
# Steam launch options wrapper: starts the game, then runs the
# BhapticsPlayer.exe stub inside the same Proton session so
# bhaptics_library's isPlayerRunning() check passes.
#
# Usage (Steam launch options):  /path/to/proton-wrap.sh %command%
#
# It appends what it did to $BHAPTICS_WRAP_LOG (default
# ~/.local/state/bhaptics-linux/proton-wrap.log), so a game that stays quiet
# can be told apart from a wrapper that never started the stub.
"$@" &
game=$!

wrap_log="${BHAPTICS_WRAP_LOG:-${XDG_STATE_HOME:-$HOME/.local/state}/bhaptics-linux/proton-wrap.log}"
log() {
  mkdir -p "$(dirname "$wrap_log")" 2>/dev/null || return 0
  printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$wrap_log" 2>/dev/null
}

proton=""
for a in "$@"; do
  case "$a" in */proton) proton="$a" ;; esac
done
if [ -n "$proton" ]; then
  log "game launched (pid $game) — starting the Player stub in 15s via ${proton##*/}"
  (
    sleep 15
    if "$proton" run 'C:\BhapticsPlayer.exe' /k rem >/dev/null 2>&1; then
      log "Player stub exited cleanly"
    else
      log "Player stub FAILED (exit $?) — the game may not believe a Player is running"
    fi
  ) &
else
  log "no */proton in the launch command — Player stub NOT started"
fi

wait "$game"
log "game exited"
