# bhaptics-linux

**bHaptics TactSuit haptic vests on Linux — no Windows, no bHaptics Player.**

A userspace daemon that talks to the vest directly over Bluetooth LE and
emulates the bHaptics Player APIs, so games, mods, VRChat, and your own
scripts can drive the vest on Linux. Verified on a **TactSuit Pro**; the
TactSuit X40 and Air speak the same protocol and should work (untested).

## Why

bHaptics only ships a Windows Player app, and it does not run under Wine.
The vest itself, though, is just a BLE device with a
[reverse-engineered protocol](https://github.com/senseshift/senseshift-firmware):
a Nordic-UART-style GATT service where 20-byte writes set 40 motor
intensities. Everything the Player does can be done natively — so this
project does it.

## What you get

- **SDK1 emulation** — `ws://127.0.0.1:15881/v2/feedbacks`. Community game
  mods and older native integrations connect and just work (including
  Windows games under Proton — localhost passes through).
- **SDK2 emulation** — `wss://127.0.0.1:15882/v3/feedback` (self-signed TLS;
  SDK2 clients skip validation by design). Handles auth, `SdkPlayDotMode`,
  `SdkPlay` with event definitions learned from the game or fetched from
  bHaptics' public definitions API.
- **VRChat OSC bridge** — `udp://127.0.0.1:9001`, official
  `bOSC/v2/VestFront|VestBack/0-19` avatar parameter convention.
- **Audio-to-haptics** — dual-band DSP on any PipeWire source: bass →
  whole-vest stereo rumble, 1–10 kHz onsets (sword hits, gunshots) → sharp
  taps on the upper chest. Per-application capture (game audio only — your
  Discord calls won't buzz), named per-game presets, auto-pauses whenever a
  real haptics client connects.
- **Web UI** — `http://127.0.0.1:15881/ui`: live motor visualization,
  battery, connected games, audio meter/tuning, an interactive motor-mapping
  wizard, and a multi-frame effect designer.
- **`vestctl` CLI** — `vestctl status | pulse | stop | audio | preset |
  effect | sources` for scripts and hotkeys.

## Install

Requirements: Linux with BlueZ + PipeWire (pactl/parec), Python 3.10+,
`openssl`. Vest paired-free — just powered on.

```sh
git clone <this repo> && cd bhaptics-linux
./install.sh
```

That creates a venv, installs deps (bleak, websockets, numpy), and sets up a
`bhaptics-daemon` systemd user service that starts at login and auto-connects
to any BLE device named `TactSuit*`/`Tactot*`. Re-run `install.sh` after
moving the folder. Open the UI, click a grid cell, feel the buzz.

If effects land on wrong body spots, run the mapping wizard in the UI (the
TactSuit Pro has 32 motors behind a 40-slot protocol; slots 32–39 are dead).

## SDK2 games under Proton

`bhaptics_library.dll` refuses to connect unless it believes the Windows
Player is installed and running. One-time fix per game (game must be closed):

```sh
tools/proton-sdk2-setup.sh <steam-appid>
```

If the mod uses an ASI loader (winmm.dll etc.), prepend
`WINEDLLOVERRIDES="winmm=n,b"` to the game's Steam launch options.

## Vibecoded, and proud of it

This project was built in one long session with
[Claude Code](https://claude.com/claude-code) (Anthropic's AI coding agent)
doing the research, protocol archaeology, coding and debugging, while a
human supplied the actual vest, pressed the buttons, and reported what their
torso felt. Every protocol path was verified against real hardware — BLE
writes, both SDK sockets, a real SDK2 game mod under Proton, OSC, and the
audio DSP. It exists because a TactSuit owner on Linux was tired of the vest
being a Windows-only accessory. Read the code before trusting it with your
hardware; that's good advice for human-written code too.

## Credits & prior art

- [SenseShift](https://github.com/senseshift/senseshift-firmware) —
  original reverse-engineering of the bHaptics BLE protocol (GATT UUIDs,
  frame format). This project would not exist without it.
- [freehaptics](https://codeberg.org/Orion_Moonclaw/freehaptics)
  (LGPL-3.0) — Rust library for bHaptics devices; the default X40 dot-grid
  mapping tables were adapted from it, and its source was an invaluable
  protocol reference.
- [godot-bhaptics-native](https://github.com/jebot-git/godot-bhaptics-native)
  (LGPL-3.0) — protocol reference.
- [bhaptics/tact-simhub](https://github.com/bhaptics/tact-simhub) — readable
  official SDK2 client, used as the wire-format reference for the SDK2
  emulation.
- [bhaptics/VRChatOSC](https://github.com/bhaptics/VRChatOSC) — the OSC
  parameter convention.

## Provenance — what was and wasn't taken

Everything here is either original, public protocol knowledge, or
license-compliant reuse:

- **No bHaptics proprietary code is included or was copied.** The SDK1/SDK2
  wire formats were learned from bHaptics' *own public GitHub repositories*
  (tact-simhub, VRChatOSC, tact-js) and from observing what their client
  library sends over the wire — standard interoperability analysis.
- The default X40 mapping tables were adapted from freehaptics (LGPL-3.0),
  which this project's GPL-3.0 license is compatible with.
- SenseShift's protocol documentation (GATT UUIDs, frame format) is used as
  published reverse-engineering research, with credit.
- One source we deliberately did **not** use: a blog post on this topic whose
  author forbids AI tools from reading it. It was skipped entirely; all
  protocol work here traces to the code repositories above.

## Disclaimer

Not affiliated with or endorsed by bHaptics Inc. "bHaptics" and "TactSuit"
are trademarks of their respective owner. This is an interoperability
project; use at your own risk.

## License

**GPL-3.0-or-later** — every derivative must remain open source with full
published source code, which also makes selling closed versions impossible.
Chosen deliberately: it's the strongest protection against closed/commercial
capture that remains compatible with the LGPL-3.0 material this project
builds on. See `LICENSE`.
