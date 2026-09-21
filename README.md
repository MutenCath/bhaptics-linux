# bhaptics-linux

[![CI](https://github.com/MutenCath/bhaptics-linux/actions/workflows/ci.yml/badge.svg)](https://github.com/MutenCath/bhaptics-linux/actions/workflows/ci.yml)

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
- **Audio-to-haptics** — multi-band DSP on any PipeWire source: bass →
  stereo rumble, 1–10 kHz onsets (sword hits, gunshots) → sharp taps on the
  upper chest. Optional **frequency spread** maps sub-bass / bass / low-mids
  to vest rows (deepest at the belly, mids at the chest) so frequency becomes
  position. Per-application capture (game audio only — your Discord calls
  won't buzz), named per-game presets, auto-pauses whenever a real haptics
  client connects.
- **Auto per-game presets** — name a preset exactly like the app in the
  source list (case-insensitive) and it applies itself, bound to that app's
  audio, the moment the game makes sound; your previous setup is restored
  when the game exits. No preset? Any new app that isn't an obvious
  non-game (browsers, Discord, media players, SteamVR itself) gets audio
  mode bound to it automatically with your current settings, so unsupported
  games just work with zero setup. Toggle in the UI or `vestctl auto on|off`.
- **Mod patterns** — import `.tact` files, SDK2 definition manifests (the
  JSON Unity/Unreal mods ship), or whole mod folders in the UI or with
  `vestctl import <dir>`; everything is pre-registered at startup under both
  SDK1 keys and SDK2 event names, so mods that play patterns by name just
  work. When a game asks for a pattern you don't have, its name shows up in
  the UI so you know exactly what to import. SDK2 event definitions learned
  from games are cached across restarts and replayable from the UI.
- **Web UI** — `http://127.0.0.1:15881/ui`: live motor visualization,
  battery, connected games, audio meter/tuning, an interactive motor-mapping
  wizard, and a multi-frame effect designer.
- **Generic haptics for unmodded Unity games** — `vestctl doctor <game>
  --generic --fix` installs the bundled **VestRumble** BepInEx plugin: it
  hooks the game's controller-rumble calls (SteamVR, Oculus, Unity XR, new
  Input System) and mirrors them to the vest as side-aware chest taps.
  Pairs with audio mode for a surprisingly complete fake integration.
  Mono Unity only for now; refuses anti-cheat games outright.
- **Gamepad rumble mirror** — for flat games: the daemon wraps your
  FF-capable gamepad in a virtual copy; rumble still reaches the pad *and*
  buzzes the belly/mid rows. No game files involved at all. Toggle in the
  UI or `vestctl pad on|off` (needs writable `/dev/uinput`; Steam setups
  usually have it).
- **`vestctl` CLI** — `vestctl status | pulse | stop | audio | auto | pad |
  preset | effect | play | import | sources | doctor | proton` for scripts
  and hotkeys.

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

Settings live in `~/.config/bhaptics-linux/` (mapping, presets, effects,
patterns) and `~/.local/state/bhaptics-linux/` (SDK2 cert + event cache);
old in-repo state migrates automatically on first start.

On Arch(-based) distros you can install system-wide instead:
`cd packaging && makepkg -si` (disable the install.sh user unit first if
you had one).

If effects land on wrong body spots, run the mapping wizard in the UI (the
TactSuit Pro has 32 motors behind a 40-slot protocol; slots 32–39 are dead).

## Game mods under Proton

One command diagnoses a game's whole bhaptics mod setup — plugin placement
(BepInEx/plugins), the client library, the Wine prefix trick, local pattern
files — and fixes what it can:

```sh
vestctl doctor <appid | name | path>          # diagnose + propose
vestctl doctor <appid | name | path> --fix    # apply the fixes
vestctl doctor --all --fix                    # one shot for the whole library
```

The **🩺 Detect games** button in the UI lists every installed game with
bhaptics support and its patch status — ✓ ready, or what's missing — with a
per-game **Fix** button, so nothing touches a game folder without you
clicking it. It distinguishes mods from built-in bhaptics support (many VR
titles ship the SDK) and native Linux builds (which need nothing). Loader DLL overrides
(BepInEx→winhttp, ASI→winmm, MelonLoader→version, UE4SS→dwmapi) are baked
into the prefix registry, so **no `WINEDLLOVERRIDES` launch option is
needed**; most mods also auto-start our fake Player stub, so usually no
launch options at all. If a mod doesn't, add
`tools/proton-wrap.sh %command%`. Background: `bhaptics_library.dll` refuses
to connect unless it believes the Windows Player is installed and running —
the doctor fakes the registry entry and drops a stub exe
(`vestctl proton <appid>` does just that part). Nothing here injects into
game processes; doctor only places mod files and writes prefix registry
keys — and it refuses to touch any game containing EasyAntiCheat/BattlEye.

For Unity games with **no** bhaptics support at all:

```sh
vestctl doctor <game> --generic --fix
```

installs BepInEx (pinned official release, SHA-256 verified, added files
only) plus the bundled VestRumble plugin — controller rumble becomes vest
taps. The plugin source lives in `plugin/VestRumble/` (~200 lines, builds
with `dotnet build`); tune strength per game in
`BepInEx/config/org.bhaptics-linux.vestrumble.cfg`.

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
