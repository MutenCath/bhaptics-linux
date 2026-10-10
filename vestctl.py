#!/usr/bin/env python3
"""Command-line control for the bhaptics-linux daemon."""
import argparse
import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import websockets

URI = "ws://127.0.0.1:15881/v2/feedbacks?app_id=ui&app_name=vestctl"
KIND_LAUNCH = "steam-launch"   # the launch-option change Steam could undo


def steam_libraries():
    roots = [Path.home() / ".steam/steam", Path.home() / ".local/share/Steam",
             Path.home() / ".var/app/com.valvesoftware.Steam/data/Steam"]
    libs = []
    for r in roots:
        vdf = r / "steamapps/libraryfolders.vdf"
        if vdf.exists():
            libs += re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(errors="ignore"))
    out = []
    for lib in libs:
        p = Path(lib)
        if p not in out and (p / "steamapps").is_dir():
            out.append(p)
    return out


def _steam_app_span(text, appid):
    """(start, end) of one appid's entry in Steam's localconfig.vdf."""
    for m in re.finditer(r'"%s"\s*\{' % re.escape(appid), text):
        start = text.index("{", m.start())
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    return start, i + 1
    return None


def _steam_app_block(text, appid):
    """Body of one appid's entry in Steam's localconfig.vdf (brace-matched)."""
    span = _steam_app_span(text, appid)
    return text[span[0]:span[1]] if span else None


def _steam_userdata_configs(roots=None):
    if roots is None:
        roots = [*steam_libraries(), Path.home() / ".steam/steam",
                 Path.home() / ".local/share/Steam"]
    for root in roots:
        for ud in sorted((root / "userdata").glob("*")):
            cfg = ud / "config" / "localconfig.vdf"
            if cfg.exists():
                yield cfg


def steam_launch_options(appid, roots=None):
    """The game's configured Steam launch options: "" when none are set, or
    None when we can't tell (no userdata, or the app isn't in there yet)."""
    for cfg in _steam_userdata_configs(roots):
        block = _steam_app_block(cfg.read_text(errors="ignore"), appid)
        if block is None:
            continue
        m = re.search(r'"LaunchOptions"\s*"([^"]*)"', block)
        return m.group(1) if m else ""
    return None


def with_player_wrapper(options, wrapper):
    """Put `wrapper` just before %command%, keeping the env assignments and
    trailing arguments the game already has. None when there's no %command%
    to anchor to (a string we shouldn't guess at)."""
    opts = options.strip()
    if not opts:
        return f"{wrapper} %command%"
    if "%command%" not in opts:
        return None
    head, _, tail = opts.partition("%command%")
    head = head.strip()
    return (f"{head} " if head else "") + wrapper + " %command%" + tail


def set_steam_launch_options(appid, value, roots=None):
    """Write one app's LaunchOptions into Steam's localconfig.vdf (atomics,
    first copy kept as a .bhaptics-bak). Returns the file, or None."""
    for cfg in _steam_userdata_configs(roots):
        text = cfg.read_text(errors="ignore")
        span = _steam_app_span(text, appid)
        if span is None:
            continue
        start, end = span
        body = text[start:end]
        m = re.search(r'("LaunchOptions"\s*)("[^"]*")', body)
        if m:
            body = body[:m.start(2)] + json.dumps(value) + body[m.end(2):]
        else:  # no key yet: add one just inside the block
            body = body.replace("{", '{\n\t\t\t\t\t\t"LaunchOptions"\t\t'
                                     + json.dumps(value), 1)
        backup = cfg.with_name(cfg.name + ".bhaptics-bak")
        if not backup.exists():
            backup.write_text(text)
        tmp = cfg.with_name(cfg.name + ".tmp")
        tmp.write_text(text[:start] + body + text[end:])
        tmp.replace(cfg)
        return str(cfg)
    return None


def steam_running():
    """Is Steam up? It rewrites localconfig.vdf from memory, so a launch-option
    edit only sticks once Steam is restarted."""
    for p in Path("/proc").glob("[0-9]*/comm"):
        try:
            if p.read_text(errors="ignore").strip().startswith("steam"):
                return True
        except OSError:
            continue
    return False


def find_game(token):
    """Match a Steam game by appid, name substring, or install path.
    Returns (appid, name, gamedir, pfx) or None."""
    tok_path = Path(token).resolve() if Path(token).exists() else None
    for lib in steam_libraries():
        sa = lib / "steamapps"
        for mf in sorted(sa.glob("appmanifest_*.acf")):
            txt = mf.read_text(errors="ignore")
            appid = re.search(r'"appid"\s+"(\d+)"', txt)
            name = re.search(r'"name"\s+"([^"]+)"', txt)
            installdir = re.search(r'"installdir"\s+"([^"]+)"', txt)
            if not (appid and installdir):
                continue
            gd = sa / "common" / installdir.group(1)
            gname = name.group(1) if name else installdir.group(1)
            plain = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
            if (token == appid.group(1)
                    or plain(token) in plain(gname)
                    or (tok_path.is_relative_to(gd.resolve()) if tok_path else False)):
                return appid.group(1), gname, gd, sa / "compatdata" / appid.group(1) / "pfx"
    return None


def find_bhaptics_dlls(gamedir):
    """Plugin DLLs that talk to bhaptics (excluding the client library itself)."""
    plugins = []
    for dll in gamedir.rglob("*.dll"):
        if dll.name.lower() == "bhaptics_library.dll":
            continue
        try:
            if dll.stat().st_size > 5_000_000:
                continue
            data = dll.read_bytes().lower()
        except OSError:
            continue
        if b"bhaptics" in data or "bhaptics".encode("utf-16-le") in data:
            plugins.append(dll)
    return plugins


def ensure_dll_overrides(reg, dlls):
    """Persist native,builtin overrides in the prefix registry — no
    WINEDLLOVERRIDES launch option needed."""
    txt = reg.read_text(errors="ignore")
    m = re.search(r'(?m)^(\[Software\\\\Wine\\\\DllOverrides\][^\n]*\n'
                  r'(?:#[^\n]*\n)*)'   # keep values after the #time line
                  r'((?:(?!\[)[^\n]*\n)*)', txt)
    if m:
        missing = [d for d in dlls if f'"{d}"' not in m.group(2)]
        if not missing:
            return
        insert = "".join(f'"{d}"="native,builtin"\n' for d in missing)
        txt = txt[:m.end(1)] + insert + txt[m.end(1):]
    else:
        txt += (f'\n[Software\\\\Wine\\\\DllOverrides] {int(time.time())}\n'
                + "".join(f'"{d}"="native,builtin"\n' for d in dlls))
    reg.write_text(txt)


def has_anticheat(gd):
    return bool(next(gd.rglob("*EasyAntiCheat*"), None)
                or next(gd.rglob("*BattlEye*"), None))


def diagnose(token):
    """Inspect one game's bhaptics setup.
    Returns (report_dict, [(todo_description, apply_callable_or_None)])."""
    hit = find_game(token)
    if hit is None:
        return None, []
    appid, name, gd, pfx = hit
    tools = Path(__file__).resolve().parent / "tools"
    if not tools.is_dir():
        tools = Path("/usr/lib/bhaptics-linux/tools")
    report = {"appid": appid, "name": name, "dir": str(gd), "notes": []}
    todo = []

    lib_dlls = list(gd.rglob("bhaptics_library.dll"))
    plugin_dlls = find_bhaptics_dlls(gd)
    if not lib_dlls and not plugin_dlls:
        report["kind"] = "none"
        return report, todo

    def is_builtin(dll):
        # inside the game's own asset tree = shipped support, not a mod
        return any(part.endswith("_Data") or part in ("Plugins", "Managed", "Content")
                   for part in dll.relative_to(gd).parts[:-1])

    mod_dlls = [d for d in plugin_dlls if not is_builtin(d)]
    native = not any(gd.rglob("*.exe"))
    report["kind"] = "mod" if mod_dlls else "builtin"
    report["native"] = native
    report["detected"] = sorted({d.name for d in mod_dlls}) or ["shipped bHaptics SDK"]
    if native:
        report["notes"].append("native Linux build — no Proton setup needed")
    if report["kind"] == "builtin":
        # a DLL inside the game's own assets only proves the game *ships* the
        # SDK. Some titles that do never call it (Arken Age) and need their
        # community mod instead — bHaptics lists those as "Mod" support, so
        # never claim this is ready without a way to check it here.
        report["notes"].append("the SDK sitting in the game's own files is not "
                               "proof the game drives the vest; bHaptics lists "
                               "each title as Native or Mod support (Mod games "
                               "need their community mod)")
        todo.append(("shipped SDK only — can't verify the game drives the "
                     "vest; check its bHaptics Native/Mod support type", None))

    report["anticheat"] = has_anticheat(gd)
    if report["anticheat"]:
        report["notes"].append("anti-cheat present (EAC/BattlEye) — leaving this "
                               "game completely untouched")
        report["launch"] = ""
        return report, []

    bepinex = (gd / "BepInEx" / "core").is_dir()
    # loader DLLs that hijack a system DLL and need a Wine override:
    # BepInEx=winhttp, ASI=winmm, MelonLoader=version, UE4SS=dwmapi
    loaders = ["winhttp"] if bepinex else []
    for dll, ov in (("winmm.dll", "winmm"), ("version.dll", "version"),
                    ("dwmapi.dll", "dwmapi")):
        if (gd / dll).exists():
            loaders.append(ov)
    if bepinex and not (gd / "winhttp.dll").exists():
        todo.append(("BepInEx doorstop winhttp.dll missing in game root — "
                     "reinstall BepInEx", None))

    # plugin dlls must live in BepInEx/plugins to be loaded
    placed = set()
    pl_dir = gd / "BepInEx" / "plugins"
    if pl_dir.is_dir():
        placed = {d.name for d in pl_dir.rglob("*.dll")}
    if bepinex:
        for dll in mod_dlls:
            if dll.name in placed or pl_dir in dll.parents:
                continue

            def _copy(src=dll):
                pl_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, pl_dir / src.name)

            todo.append((f"copy {dll.name} -> BepInEx/plugins/", _copy))
            placed.add(dll.name)  # don't queue duplicates from mod zips
    elif mod_dlls:
        report["notes"].append("no BepInEx — if the mod needs it, install "
                               "BepInEx first (winhttp.dll + BepInEx/ in game root)")

    # for mod loaders the client library must sit next to the game exe
    if mod_dlls and lib_dlls and not (gd / "bhaptics_library.dll").exists():
        def _copylib(src=lib_dlls[0]):
            shutil.copy2(src, gd / "bhaptics_library.dll")
        todo.append(("copy bhaptics_library.dll -> game root", _copylib))

    # wine prefix: registry class + player stub + loader DLL overrides
    reg = pfx / "user.reg"
    if native:
        pass  # no prefix, nothing to prep
    elif reg.exists():
        reg_txt = reg.read_text(errors="ignore")
        if not ("bhaptics-app" in reg_txt
                and (pfx / "drive_c" / "BhapticsPlayer.exe").exists()):
            def _prefix():
                subprocess.run([str(tools / "proton-sdk2-setup.sh"), appid], check=True)
            todo.append(("prep Wine prefix (fake Player registry + stub exe)", _prefix))
        missing_ov = [d for d in loaders if f'"{d}"="native,builtin"' not in reg_txt]
        if missing_ov:
            def _ov():
                ensure_dll_overrides(reg, loaders)
            todo.append((f"bake DLL override(s) into prefix registry: "
                         f"{', '.join(missing_ov)} (no launch option needed)", _ov))
    else:
        todo.append(("prefix not created yet — run the game once, then re-run doctor", None))

    # local haptic pattern files worth importing
    candidates = [f for f in gd.rglob("*")
                  if f.suffix.lower() in (".tact", ".json") and f.stat().st_size < 8_000_000
                  and re.search(rb'"[Tt]racks"|eventName|tactFileStr',
                                f.read_bytes()[:2_000_000] if f.is_file() else b"")]
    if candidates:
        def _import():
            subprocess.run([sys.executable, __file__, "import",
                            *map(str, candidates)], check=False)
        todo.append((f"import {len(candidates)} local haptic file(s): "
                     + ", ".join(f.name for f in candidates[:5]), _import))
    else:
        report["notes"].append("no local pattern files — SDK2 events are "
                               "cloud-fetched when the game first connects")

    report["launch"] = "" if native else f"{tools / 'proton-wrap.sh'} %command%"
    if report["launch"]:
        # the emulated Player stub only starts if the game runs under the
        # wrapper, and that lives in Steam's own config — check what's set
        wrapper = report["launch"].split()[0]
        wrap = Path(wrapper).name
        configured = steam_launch_options(appid)
        report["launch_set"] = configured is None or wrap in configured
        report["launch_pending"] = is_pending(appid, KIND_LAUNCH)
        if not report["launch_set"]:
            clear_pending(appid, KIND_LAUNCH)   # gone again (Steam reverted us)
            report["launch_pending"] = False
            merged = with_player_wrapper(configured, wrapper)
            report["notes"].append(
                "Steam launch options are currently "
                + (f"{configured!r}" if configured else "(none)")
                + " — games that don't auto-start the emulated Player need "
                  f"{wrap} there")
            if merged:
                def _wrap(appid=appid, value=merged):
                    set_steam_launch_options(appid, value)
                    if steam_running():
                        mark_pending(appid, KIND_LAUNCH)
                todo.append((f"add {wrap} to the game's Steam launch options "
                             "(the Player stub isn't running without it)", _wrap))
                if steam_running():
                    report["notes"].append(
                        "Steam is running and rewrites this file when it exits — "
                        "close Steam before applying, or the launch option can "
                        "be reverted")
            else:
                todo.append((f"add this to the game's Steam launch options: "
                             f"{report['launch']}", None))
        elif report["launch_pending"]:
            # we wrote it earlier: only trust it once Steam can't rewrite the
            # file, or we'd report ready on a change Steam is about to drop
            if steam_running():
                report["notes"].append(
                    "the launch option is in place, but Steam is running and "
                    "rewrites that file when it exits")
                todo.append(("close Steam, then re-run the doctor so the launch "
                             "option is kept", None))
            else:
                clear_pending(appid, KIND_LAUNCH)
                report["launch_pending"] = False
    return report, todo


def finish_report(report, todo):
    report["issues"] = [d for d, a in todo if a]
    report["manual"] = [d for d, a in todo if a is None]
    report["ok"] = not todo
    return report


def doctor(token, fix):
    report, todo = diagnose(token)
    if report is None:
        sys.exit(f"vestctl: no installed Steam game matches {token!r}")
    if report["kind"] == "none":
        sys.exit("no bhaptics mod found in the game folder — install one first "
                 "(look for bhaptics_library.dll / a *Bhaptics* plugin dll)")
    print(f"game: {report['name']} (appid {report['appid']})\n      {report['dir']}\n")
    if report["kind"] == "mod":
        print(f"[ok] bhaptics mod detected: {', '.join(report['detected'])}")
    else:
        print("[--] the game ships the bHaptics SDK — that alone doesn't prove "
              "it drives the vest")
    for note in report["notes"]:
        print(f"[--] {note}")

    fixed = 0
    if not todo:
        print("[ok] everything is in place")
    else:
        print()
        for desc, action in todo:
            if fix and action:
                action()
                fixed += 1
                print(f"[fixed] {desc}")
            else:
                print(f"[todo]  {desc}")
        if not fix and any(a for _, a in todo):
            print("\nrun again with --fix to apply the above")
    if fix and fixed:
        report, todo = diagnose(token)   # show what's left, post-fix
        if todo:
            print()
            for desc, _ in todo:
                print(f"[todo]  {desc}")
    if report["launch"]:
        state = "" if report.get("launch_set", True) else "   ← not set in Steam"
        print(f"\nSteam launch options (needed when the game doesn't "
              f"auto-start the Player):\n  {report['launch']}{state}")
    return fixed


async def rpc(messages, want=None, settle=0.0):
    async with websockets.connect(URI, open_timeout=3) as ws:
        status = json.loads(await ws.recv())
        for msg in messages:
            await ws.send(json.dumps(msg))
            reply = json.loads(await ws.recv())
            if "RegisteredKeys" in reply:
                status = reply
            if want and want in reply:
                return reply, status
        if settle:
            await asyncio.sleep(settle)
        return None, status


def iter_bhaptics_games():
    """Appids of installed games with any bhaptics traces (fast name scan)."""
    for lib in steam_libraries():
        sa = lib / "steamapps"
        for mf in sorted(sa.glob("appmanifest_*.acf")):
            txt = mf.read_text(errors="ignore")
            appid = re.search(r'"appid"\s+"(\d+)"', txt)
            installdir = re.search(r'"installdir"\s+"([^"]+)"', txt)
            if not (appid and installdir):
                continue
            gd = sa / "common" / installdir.group(1)
            if not gd.is_dir():
                continue
            names = {p.name.lower() for p in gd.rglob("*.dll")}
            if "bhaptics_library.dll" in names or any("hapt" in n for n in names):
                yield appid.group(1)


def doctor_all(fix):
    games = fixed = 0
    for appid in iter_bhaptics_games():
        games += 1
        print("=" * 62)
        try:
            fixed += doctor(appid, fix) or 0
        except SystemExit as e:
            print(e)
        print()
    print("=" * 62)
    print(f"bhaptics support found in {games} game(s), "
          + (f"{fixed} fixes applied" if fix else "use --fix to apply fixes"))


BEPINEX_URL = ("https://github.com/BepInEx/BepInEx/releases/download/"
               "v5.4.23.2/BepInEx_win_x64_5.4.23.2.zip")
BEPINEX_SHA256 = "f752ce4e838f4c305b9da1404b6745f2cff23b8bfd494f79f0c84d0a01f59b46"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME",
                                Path.home() / ".local/state")) / "bhaptics-linux"
CACHE_DIR = STATE_DIR / "cache"
# A change we made to Steam's own config isn't proven until Steam has been
# restarted: Steam keeps localconfig.vdf in memory and rewrites it on exit.
PENDING_FILE = STATE_DIR / "doctor-pending.json"


def _pending():
    try:
        data = json.loads(PENDING_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_pending(data):
    with contextlib.suppress(OSError):
        PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PENDING_FILE.with_name(PENDING_FILE.name + ".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(PENDING_FILE)


def mark_pending(appid, kind):
    """Remember that we changed something Steam can undo, until a run proves
    it stuck (that's what keeps the doctor from claiming 'ready' too early)."""
    data = _pending()
    kinds = data.setdefault(appid, [])
    if kind not in kinds:
        kinds.append(kind)
        _save_pending(data)


def clear_pending(appid, kind):
    data = _pending()
    kinds = data.get(appid)
    if kinds and kind in kinds:
        kinds.remove(kind)
        if not kinds:
            data.pop(appid, None)
        _save_pending(data)


def is_pending(appid, kind):
    return kind in _pending().get(appid, [])


def fetch_bepinex():
    """Download (once) and verify the pinned BepInEx release zip."""
    import hashlib
    import urllib.request
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    zpath = CACHE_DIR / BEPINEX_URL.rsplit("/", 1)[1]
    if not zpath.exists():
        print(f"downloading {BEPINEX_URL} ...")
        urllib.request.urlretrieve(BEPINEX_URL, zpath)
    digest = hashlib.sha256(zpath.read_bytes()).hexdigest()
    if digest != BEPINEX_SHA256:
        zpath.unlink()
        sys.exit(f"vestctl: BepInEx download hash mismatch ({digest}) — aborting")
    return zpath


def generic_profile(token, fix):
    """Install the VestRumble generic haptics profile into a Unity game."""
    hit = find_game(token)
    if hit is None:
        sys.exit(f"vestctl: no installed Steam game matches {token!r}")
    appid, name, gd, pfx = hit
    print(f"game: {name} (appid {appid})\n      {gd}\n")
    if has_anticheat(gd):
        sys.exit("anti-cheat present (EAC/BattlEye) — refusing to touch this game")
    datadirs = [d for d in gd.glob("*_Data") if d.is_dir()]
    if not datadirs:
        sys.exit("no *_Data folder — not a Unity game; generic profile is Unity-only for now")
    if (gd / "GameAssembly.dll").exists():
        sys.exit("IL2CPP Unity game — generic profile supports Mono Unity only (yet)")
    if not any((d / "Managed").is_dir() for d in datadirs):
        sys.exit("no Managed/ dir — this doesn't look like a Mono Unity game")
    if not any(gd.rglob("*.exe")):
        sys.exit("native Linux Unity build — BepInEx setup differs there; not automated yet")
    print("[ok] Mono Unity game, no anti-cheat")

    base = Path(__file__).resolve().parent
    plug_rel = Path("plugin/VestRumble/bin/Release/net46/VestRumble.dll")
    # packaged installs put vestctl in /usr/bin with the dll prebuilt in /usr/lib
    plug_dll = next((c / plug_rel for c in (base, Path("/usr/lib/bhaptics-linux"))
                     if (c / plug_rel).exists()), base / plug_rel)
    todo = []

    if not plug_dll.exists():
        if not shutil.which("dotnet"):
            sys.exit("VestRumble.dll not built and no dotnet SDK found — "
                     "install dotnet-sdk or build plugin/VestRumble first")

        def _build():
            subprocess.run(["dotnet", "build", "-c", "Release"],
                           cwd=base / "plugin" / "VestRumble", check=True)
        todo.append(("build VestRumble plugin (dotnet build)", _build))

    if not (gd / "BepInEx" / "core").is_dir():
        def _bepinex():
            import zipfile
            with zipfile.ZipFile(fetch_bepinex()) as z:
                z.extractall(gd)
        todo.append(("install BepInEx 5.4.23.2 into the game folder "
                     "(added files only)", _bepinex))

    def _plug():
        pl = gd / "BepInEx" / "plugins"
        pl.mkdir(parents=True, exist_ok=True)
        shutil.copy2(plug_dll, pl / plug_dll.name)
    todo.append(("copy VestRumble.dll -> BepInEx/plugins/", _plug))

    reg = pfx / "user.reg"
    if reg.exists():
        if '"winhttp"="native,builtin"' not in reg.read_text(errors="ignore"):
            def _ov():
                ensure_dll_overrides(reg, ["winhttp"])
            todo.append(("bake winhttp override into prefix registry", _ov))
    else:
        todo.append(("prefix not created yet — run the game once, then re-run", None))

    for desc, action in todo:
        if fix and action:
            action()
            print(f"[fixed] {desc}")
        else:
            print(f"[todo]  {desc}")
    if not fix:
        print("\nrun again with --fix to apply")
    else:
        print("\ndone — launch the game; controller rumble now taps the vest "
              "(tune in BepInEx/config/org.bhaptics-linux.vestrumble.cfg)")


def doctor_json(target, fix):
    """Machine-readable reports for the daemon/UI. target=None scans all."""
    out = []
    for appid in ([target] if target else iter_bhaptics_games()):
        report, todo = diagnose(appid)
        if report is None or report.get("kind") == "none":
            continue
        if fix:
            errors = []
            for desc, action in todo:
                if action:
                    try:
                        action()
                    except Exception as e:
                        errors.append(f"fix failed — {desc}: {e}")
            report, todo = diagnose(appid)
            report["notes"] += errors
        out.append(finish_report(report, todo))
    print(json.dumps(out))


def main():
    ap = argparse.ArgumentParser(prog="vestctl", description="Control the bHaptics vest daemon")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="vest/audio/game status")
    p = sub.add_parser("pulse", help="buzz the vest")
    p.add_argument("where", nargs="?", default="all", choices=["all", "front", "back"])
    p.add_argument("-i", "--intensity", type=int, default=60, help="0-100")
    p.add_argument("-d", "--duration", type=int, default=300, help="ms")
    sub.add_parser("stop", help="stop all effects")
    sub.add_parser("wake", help="reconnect a vest that went to sleep when idle")
    p = sub.add_parser("audio", help="audio mode on/off")
    p.add_argument("state", choices=["on", "off"])
    p = sub.add_parser("auto", help="auto per-game presets on/off")
    p.add_argument("state", choices=["on", "off"])
    p = sub.add_parser("pad", help="gamepad rumble mirror on/off")
    p.add_argument("state", choices=["on", "off"])
    p = sub.add_parser("feel", help="show/tune how effects turn into motor power")
    p.add_argument("--strength", type=int, metavar="PCT", help="master strength 25-200 %%")
    p.add_argument("--floor", type=int, metavar="N", help="minimum motor level 0-6 (0 = off)")
    p.add_argument("--smooth", choices=["on", "off"], help="dithered finer intensity steps")
    p.add_argument("--punch", choices=["on", "off"], help="20 ms kick when a motor starts")
    p.add_argument("--test", action="store_true", help="play the feel test afterwards")
    p = sub.add_parser("game", help="arm haptics for games (on = any game, off = mute)")
    p.add_argument("state", nargs="?", choices=["on", "off", "toggle"], default=None)
    p.add_argument("--forget", action="store_true",
                   help="clear the per-app settings learned for games")
    p = sub.add_parser("preset", help="list or apply presets")
    p.add_argument("name", nargs="?")
    p = sub.add_parser("effect", help="list or play saved effects")
    p.add_argument("name", nargs="?")
    p = sub.add_parser("play", help="list or play registered/imported .tact patterns")
    p.add_argument("key", nargs="?")
    p = sub.add_parser("import", help="import .tact / haptic manifest files (files or folders)")
    p.add_argument("paths", nargs="+")
    sub.add_parser("sources", help="list audio sources")
    p = sub.add_parser("proton", help="prep a Proton prefix for SDK2 game mods")
    p.add_argument("appid", help="Steam appid (or path to a Wine prefix)")
    p = sub.add_parser("doctor", help="diagnose (and --fix) a game's bhaptics mod setup")
    p.add_argument("game", nargs="?", help="Steam appid, name fragment, or game path")
    p.add_argument("--all", action="store_true", help="scan every installed Steam game")
    p.add_argument("--fix", action="store_true", help="apply the proposed fixes")
    p.add_argument("--json", action="store_true", help="machine-readable report")
    p.add_argument("--generic", action="store_true",
                   help="install the generic VestRumble profile (Unity games without bhaptics)")
    args = ap.parse_args()

    if args.cmd == "doctor":
        if args.generic:
            if not args.game:
                sys.exit("vestctl doctor --generic needs a game")
            generic_profile(args.game, args.fix)
        elif args.json:
            doctor_json(None if args.all or not args.game else args.game, args.fix)
        elif args.all:
            doctor_all(args.fix)
        elif args.game:
            doctor(args.game, args.fix)
        else:
            sys.exit("vestctl doctor: give a game, or --all")
        return

    if args.cmd == "proton":
        for script in (Path(__file__).resolve().parent / "tools" / "proton-sdk2-setup.sh",
                       Path("/usr/lib/bhaptics-linux/tools/proton-sdk2-setup.sh")):
            if script.exists():
                os.execv(str(script), [str(script), args.appid])
        sys.exit("vestctl: proton-sdk2-setup.sh not found")

    async def run():
        if args.cmd == "status":
            _, st = await rpc([])
            vest = "connected" if st["ConnectedDeviceCount"] else "NOT FOUND"
            if not st["ConnectedDeviceCount"] and st.get("VestIdle"):
                vest = "asleep (vestctl wake, or switch the vest off and on)"
            bat = f" battery {st['Battery']}%" if st.get("Battery") is not None else ""
            audio = "on" if st["AudioMode"] else "off"
            if st.get("AudioSuppressed"):
                audio += " (paused: game active)"
            auto = "on" if st.get("AudioAuto") else "off"
            if st.get("AutoProfileApp"):
                auto += f" (active: {st['AutoProfileApp']})"
            pad = "on" if st.get("PadMirror") else "off"
            if st.get("PadDevices"):
                pad += f" ({', '.join(st['PadDevices'])})"
            print(f"vest: {vest}{bat}")
            src = st["AudioSource"]
            if src == "auto":
                src = "games-only (auto)"
            print(f"audio: {audio}  source: {src}  preset: {st['ActivePreset'] or '-'}")
            print(f"auto-preset: {auto}  pad-mirror: {pad}")
            if st.get("LearnedApps"):
                print(f"learned apps: {', '.join(st['LearnedApps'])}")
            print(f"games: {', '.join(st['GameClients']) or '-'}")
            print(f"effects: {', '.join(st.get('Effects', [])) or '-'}")
            print(f"presets: {', '.join(st.get('Presets', [])) or '-'}")
        elif args.cmd == "pulse":
            dots = [{"Index": i, "Intensity": args.intensity} for i in range(20)]
            frames = []
            if args.where in ("all", "front"):
                frames.append({"Position": "VestFront", "DotPoints": dots,
                               "DurationMillis": args.duration})
            if args.where in ("all", "back"):
                frames.append({"Position": "VestBack", "DotPoints": dots,
                               "DurationMillis": args.duration})
            await rpc([{"Submit": [{"Type": "frame", "Key": f"ctl{n}", "Frame": f}
                                   for n, f in enumerate(frames)]}],
                      settle=args.duration / 1000 + 0.2)
        elif args.cmd == "stop":
            await rpc([{"Submit": [{"Type": "turnOffAll"}]}])
        elif args.cmd == "audio":
            await rpc([{"AudioMode": args.state == "on"}])
            print(f"audio {args.state}")
        elif args.cmd == "wake":
            _, st = await rpc([{"Wake": True}])
            print("waking the vest…" if st.get("VestIdle") or not st["ConnectedDeviceCount"]
                  else "vest is already awake")
        elif args.cmd == "feel":
            change = {}
            if args.strength is not None:
                change["strength"] = args.strength / 100
            if args.floor is not None:
                change["floor"] = args.floor
            if args.smooth:
                change["smooth"] = args.smooth == "on"
            if args.punch:
                change["punch"] = args.punch == "on"
            msgs = ([{"Feel": change}] if change else []) + ([{"FeelTest": True}]
                                                              if args.test else [])
            _, st = await rpc(msgs)
            f = st.get("Feel")
            if f is None:
                sys.exit("daemon too old for feel settings — restart it")
            print(f"strength {round(f['strength'] * 100)}% · minimum level "
                  f"{f['floor'] or 'off'} · smooth {'on' if f['smooth'] else 'off'}"
                  f" · punch {'on' if f['punch'] else 'off'}")
        elif args.cmd == "auto":
            await rpc([{"AudioMode": {"auto": args.state == "on"}}])
            print(f"auto per-game presets {args.state}")
        elif args.cmd == "pad":
            _, st = await rpc([{"PadMirror": args.state == "on"}])
            devs = ", ".join(st.get("PadDevices", []))
            print(f"pad mirror {args.state}" + (f" — devices: {devs}" if devs else ""))
        elif args.cmd == "game":
            if args.forget:
                await rpc([{"ForgetLearned": True}])
                print("cleared per-app learned settings")
            elif args.state is None:
                _, st = await rpc([])
                if not st["AudioMode"]:
                    print("haptics: off")
                elif st["AudioSource"] == "auto":
                    print("haptics: armed for games"
                          + (f" — active: {st['AutoProfileApp']}"
                             if st.get("AutoProfileApp") else " (waiting)"))
                else:
                    print(f"haptics: on — source {st['AudioSource']}")
                if st.get("LearnedApps"):
                    print(f"learned: {', '.join(st['LearnedApps'])}")
            else:
                target = args.state
                if target == "toggle":
                    _, st = await rpc([])
                    target = "off" if st["AudioMode"] else "on"
                if target == "on":
                    await rpc([{"AudioMode": {"enabled": True, "auto": True,
                                              "source": "auto"}}])
                    print("haptics armed — any game that plays sound gets the vest")
                else:
                    await rpc([{"AudioMode": False}])
                    print("haptics off")
        elif args.cmd == "preset":
            if args.name:
                _, st = await rpc([{"ApplyPreset": args.name}])
                ok = st.get("ActivePreset") == args.name
                print(f"preset {'applied' if ok else 'NOT FOUND'}: {args.name}")
                sys.exit(0 if ok else 1)
            _, st = await rpc([])
            print("\n".join(st.get("Presets", [])) or "(none)")
        elif args.cmd == "effect":
            if args.name:
                await rpc([{"PlayEffect": args.name}], settle=0.3)
            else:
                _, st = await rpc([])
                print("\n".join(st.get("Effects", [])) or "(none)")
        elif args.cmd == "play":
            if args.key:
                await rpc([{"Submit": [{"Type": "key", "Key": args.key}]}], settle=0.5)
            else:
                _, st = await rpc([])
                print("\n".join(st.get("RegisteredKeys", [])) or "(none)")
        elif args.cmd == "import":
            files = []
            for raw in args.paths:
                p_ = Path(raw)
                if p_.is_dir():
                    files += sorted(f for f in p_.rglob("*")
                                    if f.suffix.lower() in (".tact", ".json")
                                    and f.stat().st_size < 8_000_000)
                elif p_.is_file():
                    files.append(p_)
            msgs = []
            for f in files:
                try:
                    data = json.loads(f.read_text(errors="ignore"))
                except (OSError, ValueError):
                    continue
                msgs.append({"ImportTact": {"name": f.stem, "project": data}})
            if not msgs:
                sys.exit("vestctl: no readable .tact/.json files found")
            async with websockets.connect(URI, open_timeout=3) as ws:
                await ws.recv()  # initial status
                for m in msgs:
                    await ws.send(json.dumps(m))
                got = 0
                while got < len(msgs):
                    r = json.loads(await ws.recv())
                    if "ImportNote" in r:
                        print(r["ImportNote"])
                        got += 1
        elif args.cmd == "sources":
            reply, _ = await rpc([{"ListAudioSources": True}], want="AudioSources")
            for s in reply["AudioSources"]:
                print(f"{s['value']:44s} {s['label']}")

    try:
        asyncio.run(run())
    except (OSError, asyncio.TimeoutError):
        print("vestctl: daemon unreachable on ws://127.0.0.1:15881 — is bhaptics-daemon running?",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
