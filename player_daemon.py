"""bHaptics Player emulator for Linux: SDK1 WebSocket API -> direct BLE.

Listens on ws://127.0.0.1:15881/v2/feedbacks (the endpoint bHaptics-enabled
games and mods connect to) and drives a TactSuit over BLE, no Windows Player
needed. Also serves a test/mapping UI at http://127.0.0.1:15881/ui (loopback
only). All sockets bind loopback by default; set BHAPTICS_BIND (or
~/.config/bhaptics-linux/network.json {"bind": ...}) to expose them on the LAN
for Remote Play — see tools/remote-relay.py and the README.

Standard protocol: Register (.tact projects), Submit type=key/frame/turnOff/
turnOffAll, dot+path effects on VestFront/VestBack. Non-vest positions are
accepted but ignored.

Extensions (used by the UI, ignored by real SDK clients):
  {"Submit":[{"Type":"raw","Frame":{"Motors":[40x 0-100],"DurationMillis":ms}}]}
  {"SetMapping":{"front":[20 ints 0-39],"back":[20 ints 0-39]}}
  {"AudioMode": true|false|{"enabled":bool,"gain":f,"floor":f,"max":int}}
Status replies additionally carry "Mapping" and "AudioMode".
"""
import asyncio
import base64
import contextlib
import errno
import http
import json
import logging
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import websockets
from bleak import BleakClient, BleakScanner

try:
    import evdev
    from evdev import ecodes as EC
except ImportError:  # pad mirror simply stays unavailable
    evdev = None

log = logging.getLogger("bhaptics-daemon")

__version__ = "0.0.2"

BASE_DIR = Path(__file__).resolve().parent
UI_FILE = BASE_DIR / "ui.html"

# user files live in XDG dirs so the daemon can run from a read-only install
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME",
                                 Path.home() / ".config")) / "bhaptics-linux"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME",
                                Path.home() / ".local/state")) / "bhaptics-linux"
MAPPING_FILE = CONFIG_DIR / "mapping.json"
FEEL_FILE = CONFIG_DIR / "feel.json"
PATTERNS_DIR = CONFIG_DIR / "patterns"
NETWORK_FILE = CONFIG_DIR / "network.json"


def migrate_legacy_state():
    """One-time move of state files from the repo dir (pre-XDG layout)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    moves = [
        (BASE_DIR / n, CONFIG_DIR / n)
        for n in ("mapping.json", "audio_settings.json", "audio_presets.json",
                  "effects.json", "patterns")
    ] + [
        (BASE_DIR / n, STATE_DIR / n)
        for n in ("sdk2_cache", "sdk2_cert.pem", "sdk2_key.pem")
    ]
    for old, new in moves:
        if old.exists() and not new.exists():
            shutil.move(str(old), str(new))
            log.info("migrated %s -> %s", old.name, new)

def bind_address():
    """Address the SDK/OSC sockets listen on, default 127.0.0.1 (matching the
    official bHaptics Player). Set BHAPTICS_BIND or network.json {"bind": ...}
    to a LAN address (e.g. 0.0.0.0) to drive the vest from a game running on
    another PC (Remote Play) via tools/remote-relay.py. These endpoints are
    unauthenticated, so only bind beyond loopback on a trusted network."""
    env = os.environ.get("BHAPTICS_BIND")
    if env:
        return env
    try:
        value = json.loads(NETWORK_FILE.read_text())["bind"]
    except (OSError, ValueError, KeyError, TypeError):
        return "127.0.0.1"
    return str(value)


def remote_play_enabled():
    return bind_address() not in ("127.0.0.1", "::1", "localhost")


def set_remote_play(enabled):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    NETWORK_FILE.write_text(json.dumps({"bind": "0.0.0.0" if enabled else "127.0.0.1"}))


def _rebind():
    log.info("re-binding listeners (remote play)")
    os.execv(sys.executable, [sys.executable, *sys.argv])

VEST_NAME_PREFIXES = ("TactSuit", "Tactot")
MOTOR_STABLE = "6e40000a-b5a3-f393-e0a9-e50e24dcca9e"
TICK_MS = 20

# Player dot index (row-major 4x5 grid) -> BLE frame motor index, X40 layout.
# The Pro/new-gen firmware exposes the same 40-slot frame.
DEFAULT_FRONT = [31, 30, 1, 0, 33, 32, 3, 2, 35, 34, 5, 4, 37, 36, 7, 6, 39, 38, 9, 8]
DEFAULT_BACK = [10, 11, 20, 21, 12, 13, 22, 23, 14, 15, 24, 25, 16, 17, 26, 27, 18, 19, 28, 29]


def pack(motors40):
    return bytes(
        (min(int(motors40[i * 2]), 15) << 4) | min(int(motors40[i * 2 + 1]), 15)
        for i in range(20)
    )


def grid_to_dot(x, y):
    """Map path point x,y in [0,1] to nearest dot index on the 4x5 grid."""
    col = min(3, max(0, int(x * 4)))
    row = min(4, max(0, int(y * 5)))
    return row * 4 + col


PATH_RADIUS = 0.3  # falloff distance (grid units; motors are 0.25 x 0.2 apart)


def spread_point(target, x, y, intensity, motor_count=3):
    """Render a path point the way the Player does: a soft spot over the
    nearest motors (intensity falling off with distance) instead of snapping
    to one motor — so moving paths glide instead of jumping cell to cell."""
    x = min(1.0, max(0.0, x))
    y = min(1.0, max(0.0, y))
    near = sorted(
        (((c + 0.5) / 4 - x) ** 2 + ((r + 0.5) / 5 - y) ** 2, r * 4 + c)
        for r in range(5) for c in range(4))[:max(1, int(motor_count or 3))]
    weights = [(max(0.0, 1.0 - d2 ** 0.5 / PATH_RADIUS), i) for d2, i in near]
    # every point is < 0.17 from a motor centre, so the closest weight is > 0;
    # scaling by it gives the closest motor(s) the full intensity
    top = weights[0][0]
    for w, i in weights:
        target[i] = max(target[i], intensity * w / top)


def path_at(pts, rel):
    """Position/intensity of a path at `rel` ms, interpolated between the
    surrounding keyframes."""
    prev = pts[0]
    for p in pts:
        t = int(g(p, "Time", 0))
        if t > rel:
            t0 = int(g(prev, "Time", 0))
            if t <= t0:
                break
            k = (rel - t0) / (t - t0)
            # normalise before blending: 0..100 ints near 0 would otherwise
            # read as 0..1 floats mid-way
            a = (float(g(prev, "X", 0)), float(g(prev, "Y", 0)),
                 norm_intensity(g(prev, "Intensity", 0)))
            b = (float(g(p, "X", 0)), float(g(p, "Y", 0)),
                 norm_intensity(g(p, "Intensity", 0)))
            return tuple(u + (v - u) * k for u, v in zip(a, b))
        prev = p
    return (float(g(prev, "X", 0)), float(g(prev, "Y", 0)),
            norm_intensity(g(prev, "Intensity", 0)))


def norm_intensity(v):
    """tact files use 0..1 floats; frame submits use 0..100 ints."""
    v = float(v)
    return v / 100.0 if v > 1.0 else v


def g(d, key, default=None):
    """Tolerant getter: portal/SDK2 exports use camelCase, .tact uses PascalCase."""
    if key in d:
        return d[key]
    alt = key[0].lower() + key[1:]
    return d.get(alt, default)


class Feel:
    """Output stage: turns 0..1 motor levels into the vest's 4-bit values.

    - strength: master intensity for everything the vest plays
    - floor: lowest level a motor is driven at when it should be on at all
      (coin motors don't start below a few steps, so weak effects vanished)
    - smooth: error-diffusion dithering across 20 ms ticks, so the motors'
      inertia averages neighbouring steps into ~4x finer intensity
    - punch: one full-power tick when a motor starts from rest, spinning
      the motor up faster so taps land crisp instead of mushy
    """

    DEFAULTS = {"strength": 1.0, "floor": 0, "smooth": True, "punch": False}

    def __init__(self):
        self.strength = 1.0
        self.floor = 0
        self.smooth = True
        self.punch = False
        self._err = [0.0] * 40
        self._prev = [0] * 40
        with contextlib.suppress(OSError, ValueError, TypeError):
            self.update(json.loads(FEEL_FILE.read_text()), save=False)

    def as_dict(self):
        return {"strength": self.strength, "floor": self.floor,
                "smooth": self.smooth, "punch": self.punch}

    def update(self, d, save=True):
        self.strength = min(2.0, max(0.25, float(d.get("strength", self.strength))))
        self.floor = min(6, max(0, int(d.get("floor", self.floor))))
        self.smooth = bool(d.get("smooth", self.smooth))
        self.punch = bool(d.get("punch", self.punch))
        if save:
            with contextlib.suppress(OSError):
                atomic_write_json(FEEL_FILE, self.as_dict())

    def shape(self, levels):
        """levels: 40 floats 0..1 (physical motor order) -> 40 ints 0..15."""
        out = [0] * 40
        span = 15 - self.floor
        for i, v in enumerate(levels):
            if v <= 0.004:  # off stays exactly off (no dither noise)
                self._err[i] = 0.0
                self._prev[i] = 0
                continue
            want = self.floor + min(1.0, v * self.strength) * span
            if self.smooth:
                want += self._err[i]
                q = min(15, max(1 if self.floor else 0, int(want + 0.5)))
                self._err[i] = max(-1.0, min(1.0, want - q))
            else:
                q = min(15, max(1 if self.floor else 0, round(want)))
            if self.punch and q and not self._prev[i]:
                q = 15  # kick-start from rest; next tick settles to the level
            out[i] = q
            self._prev[i] = q
        return out


class VestMapping:
    def __init__(self):
        self.front = DEFAULT_FRONT[:]
        self.back = DEFAULT_BACK[:]
        if MAPPING_FILE.exists():
            try:
                data = json.loads(MAPPING_FILE.read_text())
                self.set(data["front"], data["back"], save=False)
                log.info("loaded mapping from %s", MAPPING_FILE)
            except Exception as e:
                log.warning("ignoring bad %s: %s", MAPPING_FILE, e)

    def set(self, front, back, save=True):
        front = [int(v) for v in front]
        back = [int(v) for v in back]
        if len(front) != 20 or len(back) != 20 or not all(0 <= v < 40 for v in front + back):
            raise ValueError("mapping must be two lists of 20 motor indices in 0-39")
        self.front, self.back = front, back
        if save:
            atomic_write_json(MAPPING_FILE, {"front": front, "back": back})
            log.info("mapping saved")

    def as_dict(self):
        return {"front": self.front, "back": self.back}


class Effect:
    """A compiled timeline of per-tick entries:
    ("mapped", front20, back20) with 0..1 floats, or ("raw", motors40)."""

    def __init__(self, key, timeline):
        self.key = key
        self.timeline = timeline
        self.start = time.monotonic()

    def current(self):
        idx = int((time.monotonic() - self.start) * 1000 / TICK_MS)
        if idx >= len(self.timeline):
            return None
        return self.timeline[idx]


def compile_frame_submit(frame_msg):
    duration = int(frame_msg.get("DurationMillis", 100)) or 100
    position = frame_msg.get("Position", "Vest")
    front = [0.0] * 20
    back = [0.0] * 20

    def apply(target, dots, paths):
        for d in dots or []:
            i = int(d.get("Index", 0))
            if 0 <= i < 20:
                target[i] = max(target[i], norm_intensity(d.get("Intensity", 0)))
        for p in paths or []:
            spread_point(target, float(p.get("X", 0)), float(p.get("Y", 0)),
                         norm_intensity(p.get("Intensity", 0)), p.get("MotorCount", 3))

    dots, paths = frame_msg.get("DotPoints"), frame_msg.get("PathPoints")
    if position in ("VestFront", "Vest"):
        apply(front, dots, paths)
    if position == "VestBack":
        apply(back, dots, paths)
    if position == "Vest" and dots:
        # 'Vest' position addresses 40 dots: 0-19 front, 20-39 back
        for d in dots:
            i = int(d.get("Index", 0))
            if 20 <= i < 40:
                back[i - 20] = max(back[i - 20], norm_intensity(d.get("Intensity", 0)))

    ticks = max(1, duration // TICK_MS)
    return [("mapped", front, back)] * ticks


def compile_raw_submit(frame_msg):
    duration = int(frame_msg.get("DurationMillis", 100)) or 100
    motors = [norm_intensity(v) for v in (frame_msg.get("Motors") or [])][:40]
    motors += [0.0] * (40 - len(motors))
    ticks = max(1, duration // TICK_MS)
    return [("raw", motors)] * ticks


def compile_project(project, intensity_ratio=1.0, duration_ratio=1.0):
    """Flatten a .tact project into per-tick entries."""
    tracks = g(project, "Tracks", [])
    events = []  # (start_ms, end_ms, position, kind, payload)
    total_end = 0

    for track in tracks:
        for effect in g(track, "Effects", []):
            base = int(g(effect, "StartTime", 0) + g(effect, "OffsetTime", 0))
            for position, modes in (g(effect, "Modes") or {}).items():
                if position not in ("VestFront", "VestBack"):
                    continue
                dot_mode = g(modes, "DotMode") or {}
                for fb in g(dot_mode, "Feedback", []):
                    s = base + int(g(fb, "StartTime", 0))
                    e = base + int(g(fb, "EndTime", g(fb, "StartTime", 0) + 100))
                    pts = [
                        (int(g(p, "Index", 0)), norm_intensity(g(p, "Intensity", 0)))
                        for p in g(fb, "PointList", [])
                    ]
                    if pts and e > s:
                        events.append((s, e, position, "dot", pts))
                        total_end = max(total_end, e)
                path_mode = g(modes, "PathMode") or {}
                for fb in g(path_mode, "Feedback", []):
                    pts = g(fb, "PointList", [])
                    if not pts:
                        continue
                    times = [int(g(p, "Time", 0)) for p in pts]
                    s, e = base + min(times), base + max(max(times), min(times) + 100)
                    events.append((s, e, position, "path", pts))
                    total_end = max(total_end, e)

    total_end = int(total_end * duration_ratio)
    if total_end <= 0:
        return []

    n_ticks = max(1, total_end // TICK_MS)
    timeline = []
    for tick in range(n_ticks):
        t = tick * TICK_MS / duration_ratio if duration_ratio else tick * TICK_MS
        front = [0.0] * 20
        back = [0.0] * 20
        for s, e, position, kind, payload in events:
            if not (s <= t < e):
                continue
            target = front if position == "VestFront" else back
            if kind == "dot":
                for i, inten in payload:
                    if 0 <= i < 20:
                        target[i] = max(target[i], inten * intensity_ratio)
            else:  # path: glide between keyframes, soft spot over nearby motors
                x, y, inten = path_at(payload, t - s)
                spread_point(target, x, y, inten * intensity_ratio)
        timeline.append(("mapped", front, back))
    return timeline


CLIP_FRAME_MS = 20  # SDK2 audio-derived clips: one 20-motor frame per 20 ms


def _decode_clip_frame(b64):
    """20 bytes = SDK1 dot order (row*4 + col), 0..100; >100 is padding
    (255 fills the unused last byte in most clips)."""
    try:
        raw = base64.b64decode(b64)[:20]
    except (ValueError, TypeError):
        raw = b""
    vals = [0.0 if v > 100 else v / 100.0 for v in raw]
    return vals + [0.0] * (20 - len(vals))


def clip_project(mapping):
    """SDK2 mappings authored from audio carry `audioFilePatterns` clips
    instead of .tact tracks; keep the vest frames as a compact project."""
    clips = []
    for ap in mapping.get("audioFilePatterns") or []:
        pats = (ap.get("clip") or {}).get("patterns") or {}
        front, back = pats.get("VestFront") or [], pats.get("VestBack") or []
        if front or back:
            clips.append((front, back))
    if not clips:
        return None
    try:
        scale = float(mapping.get("intensity", 100)) / 100.0
    except (TypeError, ValueError):
        scale = 1.0
    return {"_clips": clips, "_scale": scale}


def compile_clip(project, intensity_ratio=1.0, duration_ratio=1.0):
    # several clips on one event are layers authored to play together
    # (e.g. "death" = dying heartbeat + a cough; the event's eventTime is the
    # longest layer): combine them per motor with max so nothing overdrives
    scale = project.get("_scale", 1.0) * intensity_ratio
    n = max(max(len(f), len(b)) for f, b in project["_clips"])
    ratio = duration_ratio if duration_ratio > 0 else 1.0
    n_ticks = max(1, round(n * CLIP_FRAME_MS * ratio / TICK_MS))
    decoded = {}

    def frame(frames, i):
        key = (id(frames), i)
        if key not in decoded:
            decoded[key] = [min(1.0, v * scale) for v in _decode_clip_frame(frames[i])]
        return decoded[key]

    timeline = []
    for tick in range(n_ticks):
        i = min(n - 1, int(tick * TICK_MS / ratio / CLIP_FRAME_MS))
        front, back = [0.0] * 20, [0.0] * 20
        for fr, bk in project["_clips"]:
            for frames, out in ((fr, front), (bk, back)):
                if i < len(frames):
                    for m, v in enumerate(frame(frames, i)):
                        if v > out[m]:
                            out[m] = v
        timeline.append(("mapped", front, back))
    return timeline


def compile_event(project, intensity_ratio=1.0, duration_ratio=1.0):
    """Compile an SDK2 event definition (.tact project or audio clip)."""
    if isinstance(project, dict) and "_clips" in project:
        return compile_clip(project, intensity_ratio, duration_ratio)
    return compile_project(project, intensity_ratio, duration_ratio)


def event_duration_ms(project):
    """Length of an event without compiling it (for ServerEventList)."""
    if isinstance(project, dict) and "_clips" in project:
        return max(max(len(f), len(b)) for f, b in project["_clips"]) * CLIP_FRAME_MS
    return len(compile_project(project)) * TICK_MS


def feel_test_timeline():
    """~3 s probe for the Feel settings: a slow whole-vest swell (smoothness,
    strength), then weak and strong taps (minimum level, punch)."""
    tl = []
    for k in range(75):  # 1.5 s swell up and down
        v = 1.0 - abs(k - 37) / 37
        tl.append(("mapped", [v] * 20, [v] * 20))
    tl += [("mapped", [0.0] * 20, [0.0] * 20)] * 10
    for lvl in (0.08, 0.15, 0.3, 0.6, 1.0):  # taps, weak to strong
        tap = [0.0] * 20
        for i in (5, 6, 9, 10):  # centre chest
            tap[i] = lvl
        tl += [("mapped", tap, [0.0] * 20)] * 4 + [("mapped", [0.0] * 20, [0.0] * 20)] * 8
    return tl


def parse_osc(data):
    """Minimal OSC parser: returns [(address, args)], handles bundles."""
    msgs = []

    def read_str(b, i):
        end = b.index(0, i)
        return b[i:end].decode("ascii", "replace"), (end + 4) & ~3

    def parse_msg(b):
        try:
            addr, i = read_str(b, 0)
            if not addr.startswith("/"):
                return
            tags, i = read_str(b, i)
            args = []
            for t in tags[1:]:
                if t == "f":
                    args.append(struct.unpack_from(">f", b, i)[0]); i += 4
                elif t == "i":
                    args.append(struct.unpack_from(">i", b, i)[0]); i += 4
                elif t == "T":
                    args.append(1.0)
                elif t == "F":
                    args.append(0.0)
                elif t == "s":
                    s, i = read_str(b, i); args.append(s)
                else:
                    return
            msgs.append((addr, args))
        except (ValueError, struct.error):
            pass

    def walk(b):
        if b[:8] == b"#bundle\x00":
            i = 16
            while i + 4 <= len(b):
                (size,) = struct.unpack_from(">i", b, i)
                i += 4
                walk(b[i:i + size])
                i += size
        else:
            parse_msg(b)

    walk(data)
    return msgs


class OscProtocol(asyncio.DatagramProtocol):
    def __init__(self, state):
        self.state = state
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if self.state.relay_host and self.state.host_remote:
            with contextlib.suppress(Exception):
                self.transport.sendto(data, (self.state.host_remote, OSC_PORT))
        for address, args in parse_osc(data):
            if address == "/vest/tap":
                # generic-profile rumble (VestRumble plugin / scripts);
                # deliberately does NOT count as a haptics client, so
                # audio mode keeps running alongside
                with contextlib.suppress(Exception):
                    self.state.vest_tap(args)
                continue
            m = OSC_RE.match(address)
            if not m or not args:
                continue
            idx = int(m.group(2))
            if idx > 19:
                continue
            try:
                val = float(args[0])
            except (TypeError, ValueError):
                continue
            target = (self.state.osc_front if m.group(1) == "VestFront"
                      else self.state.osc_back)
            target[idx] = min(1.0, max(0.0, val))
            self.state.osc_last = time.monotonic()


BATTERY_CHAR = "6e400008-b5a3-f393-e0a9-e50e24dcca9e"


class VestLink:
    def __init__(self):
        self.client = None
        self.last_sent = None
        self.battery = None
        self.last_active = time.monotonic()
        self.idle = False  # slept after IDLE_DISCONNECT_SECS idle to save battery
        self._batt_tick = 0
        self._absent_scans = 0  # while asleep: scans in a row that missed the vest

    @property
    def connected(self):
        return self.client is not None and self.client.is_connected

    def mark_active(self):
        """Haptics are being produced: keep/return the vest awake."""
        self.last_active = time.monotonic()
        if self.idle:
            self.idle = False
            log.info("vest wake: activity resumed, reconnecting")

    async def _sleep_vest(self):
        log.info("vest idle for %d min — disconnecting to save the battery",
                 IDLE_DISCONNECT_SECS // 60)
        self.idle = True
        self._absent_scans = 0
        dead, self.client = self.client, None
        self.last_sent = None
        with contextlib.suppress(Exception):
            await dead.write_gatt_char(MOTOR_STABLE, bytes(20), response=False)
            await dead.disconnect()

    @staticmethod
    async def _find(timeout):
        return await BleakScanner.find_device_by_filter(
            lambda d, adv: (d.name or "").startswith(VEST_NAME_PREFIXES),
            timeout=timeout,
        )

    async def _watch_while_asleep(self):
        """Asleep, we don't connect — but the vest going away (it powers
        itself off once nothing is connected) and then advertising again
        means someone switched it back on: that's a wake-up. A vest that
        just stays on after we let go doesn't count, or it would never sleep."""
        try:
            present = await self._find(IDLE_SCAN_SECS) is not None
        except Exception:  # e.g. BlueZ busy with another app's scan
            present = None
        if present is False:
            self._absent_scans += 1
        elif present:
            if self._absent_scans >= 2 and self.idle:  # gone ≥2 scans, now back
                log.info("vest wake: switched back on")
                self.mark_active()
                return
            self._absent_scans = 0  # still on since we let go: keep sleeping
        await asyncio.sleep(IDLE_SCAN_SECS)

    async def maintain(self):
        while True:
            if not self.connected:
                if self.idle:
                    await self._watch_while_asleep()
                    continue
                try:
                    device = await self._find(10.0)
                    if device is None:
                        await asyncio.sleep(3)
                        continue
                    client = BleakClient(device, timeout=20.0)
                    await client.connect()
                    self.client = client
                    self.last_sent = None
                    self.last_active = time.monotonic()
                    await self.read_battery()
                    log.info("vest connected: %s (%s), battery %s%%",
                             device.name, device.address, self.battery)
                except Exception as e:
                    log.warning("vest connect failed: %s", e)
                    self.client = None
                    self.battery = None
                    await asyncio.sleep(3)
            else:
                await asyncio.sleep(1)
                if (IDLE_DISCONNECT_SECS
                        and time.monotonic() - self.last_active > IDLE_DISCONNECT_SECS):
                    await self._sleep_vest()
                    continue
                self._batt_tick += 1
                if self._batt_tick >= 60:
                    self._batt_tick = 0
                    await self.read_battery()

    async def read_battery(self):
        if not self.connected:
            return
        with contextlib.suppress(Exception):
            data = await self.client.read_gatt_char(BATTERY_CHAR)
            if data:
                prev = self.battery if self.battery is not None else 100
                self.battery = data[0]
                if self.battery < 15 <= prev:
                    notify("bHaptics vest",
                           f"Battery at {self.battery}% — plug the vest in",
                           urgency="critical", icon="battery-caution")

    async def send(self, motors40):
        if not self.connected:
            return
        data = pack(motors40)
        if data == self.last_sent:
            return
        try:
            await self.client.write_gatt_char(MOTOR_STABLE, data, response=False)
            self.last_sent = data
        except Exception as e:
            log.warning("BLE write failed: %s", e)
            dead, self.client = self.client, None
            # release the BlueZ connection so the reconnect scan can find the vest
            with contextlib.suppress(Exception):
                await dead.disconnect()


AUDIO_RATE = 48000
AUDIO_CHUNK = 960  # 20 ms
AUDIO_CONF = CONFIG_DIR / "audio_settings.json"
PRESETS_FILE = CONFIG_DIR / "audio_presets.json"
LEARNED_FILE = CONFIG_DIR / "audio_learned.json"
MIGRATED_MARK = CONFIG_DIR / ".games-only-default"
EFFECTS_FILE = CONFIG_DIR / "effects.json"
# per-app learning: remembered settings keyed by app name, so a game you
# tuned once comes back tuned — no preset naming required.
# app-wide toggles: never stored in presets/learned tuning, never reverted by
# a game-exit restore (turning notifications off mid-game must stick)
GLOBAL_KEYS = ("enabled", "auto", "pad", "notify")
LEARN_SKIP = GLOBAL_KEYS + ("source",)
AUTOPROFILE_POLL = 4.0  # s between auto-follow scans of playing apps
SDK1_PORT = 15881  # bHaptics Player SDK1 websocket + web UI (fixed by the SDK)
SDK2_PORT = 15882  # SDK2 wss (fixed by the SDK)
OSC_PORT = 9001  # VRChat sends avatar parameters here
DISCOVERY_PORT = 15880  # LAN discovery probe/response for tools/remote-relay.py
OSC_RE = re.compile(r"^/avatar/parameters/bOSC/v2/(VestFront|VestBack)/(\d+)$")
GAME_ACTIVE_WINDOW = 8.0  # s: while a game sends patterns, audio mode yields
IDLE_DISCONNECT_SECS = 900  # s: sleep the vest after this long with no haptics
IDLE_SCAN_SECS = 5.0  # s: asleep, scan this long (then pause as long) for a power-on


SUR_SINK = "vest51"
SUR_MAP = "front-left,front-right,front-center,lfe,rear-left,rear-right"


def pactl(*args):
    """Blocking — call from an executor thread, never on the event loop
    (it would stall the 20 ms mixer tick). Returns "" on failure/hang."""
    try:
        return subprocess.run(["pactl", *args], capture_output=True,
                              text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def sink_inputs():
    """Parsed `pactl list sink-inputs`; [] when pactl fails."""
    try:
        return json.loads(pactl("-f", "json", "list", "sink-inputs"))
    except ValueError:
        return []


def strip_global(d):
    return {k: v for k, v in d.items() if k not in GLOBAL_KEYS}


def atomic_write_json(path, obj, indent=None):
    """Write-then-rename so a crash mid-write can't truncate a config file
    (the loaders treat unreadable JSON as empty and the next save wipes it)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=indent))
    tmp.replace(path)


def notify(title, body, urgency="normal", icon="input-gaming", replace=None):
    """Desktop notification via notify-send, reusing one slot when `replace`
    is given so a flapping vest or repeated game rebinds don't stack up."""
    if not shutil.which("notify-send"):
        return
    cmd = ["notify-send", "-a", "bHaptics", "-u", urgency]
    if icon:
        cmd += ["-i", icon]
    if replace:
        cmd += ["-h", f"string:x-canonical-private-synchronous:{replace}"]
    cmd += [title, body]

    def run():
        with contextlib.suppress(Exception):
            subprocess.run(cmd, capture_output=True, timeout=10)

    threading.Thread(target=run, daemon=True).start()  # reaps notify-send


class AudioEngine:
    """Captures the default sink monitor, exposes bass level as 0..1."""

    def __init__(self):
        self.enabled = False
        self.stereo = True
        self.level_l = 0.0
        self.level_r = 0.0
        # per-band outputs when spread mode is on: [sub, bass, lowmid]
        self.band_l = [0.0, 0.0, 0.0]
        self.band_r = [0.0, 0.0, 0.0]
        self.spread = False  # map bands to vest rows (low = belly, mid = chest)
        self.rel = 0.0  # relative loudness 0..1, for the UI meter
        self.thresh = 0.55
        self.gain = 4.0  # sensitivity: higher -> lower beat threshold
        self.floor = 0.015
        self.max_level = 8  # motor intensity cap 1-15
        self.bass_hz = 250.0
        self.impact = True  # HF transient detection (sword hits, gunshots)
        self.impact_gain = 5.0
        self.impact_max = 6
        self.impact_l = 0.0
        self.impact_r = 0.0
        self.agc = True  # auto-scale: adapt thresholds to the game's mix
        self.activity = 5.0  # 1-10 target: how busy the vest should feel
        self.duty = 0.0  # measured fraction of time the vest is rumbling
        self.surround = False  # 5.1 capture: rear channels drive the back panel
        self.rear_live = False  # rear channels actually carrying audio
        self.level_bl = self.level_br = 0.0
        self.band_bl = [0.0, 0.0, 0.0]
        self.band_br = [0.0, 0.0, 0.0]
        self.impact_bl = self.impact_br = 0.0
        self._task = None
        self._proc = None
        self.source = "auto"  # bind to whatever game is playing; silence otherwise
        self.start_enabled = True
        self.auto_profiles = True
        self.pad_mirror = False
        self.notify = True
        self.learned = {}  # app key (lowercase) -> remembered settings
        if LEARNED_FILE.exists():
            with contextlib.suppress(Exception):
                self.learned = json.loads(LEARNED_FILE.read_text())
        if AUDIO_CONF.exists():
            with contextlib.suppress(Exception):
                c = json.loads(AUDIO_CONF.read_text())
                self.gain = float(c.get("gain", self.gain))
                self.floor = float(c.get("floor", self.floor))
                self.max_level = int(c.get("max", self.max_level))
                self.stereo = bool(c.get("stereo", self.stereo))
                self.start_enabled = bool(c.get("enabled", True))
                self.source = str(c.get("source", "default"))
                self.impact = bool(c.get("impact", True))
                self.impact_gain = float(c.get("impGain", self.impact_gain))
                self.impact_max = int(c.get("impMax", self.impact_max))
                self.auto_profiles = bool(c.get("auto", True))
                self.spread = bool(c.get("spread", False))
                self.pad_mirror = bool(c.get("pad", False))
                self.agc = bool(c.get("agc", self.agc))
                self.activity = float(c.get("activity", self.activity))
                self.surround = bool(c.get("surround", False))
                self.notify = bool(c.get("notify", self.notify))
        # legacy raw stream indexes don't survive reboots (appname: does)
        if self.source.startswith("app:"):
            self.source = "default"
        # one-time: a source pinned to a single app is what made "audio on"
        # feel like "off" for every other game. remember that app's tuning,
        # then switch to games-only auto. the marker keeps an intentional
        # per-app pin from being rewritten on later restarts.
        first_run = not MIGRATED_MARK.exists()
        if first_run and self.source.startswith("appname:") and self.auto_profiles:
            app = self.source[len("appname:"):].lower()
            self.learned.setdefault(app, self._snapshot())
            self._save_learned()
            self.source = "auto"
            with contextlib.suppress(OSError, ValueError):
                c = json.loads(AUDIO_CONF.read_text())
                c["source"] = "auto"
                atomic_write_json(AUDIO_CONF, c)
            log.info("audio: learned settings for %r, switched to games-only auto", app)
        if first_run:
            with contextlib.suppress(OSError):
                MIGRATED_MARK.touch()
        self.waiting = False
        # PlayerState points this at the vest. An idle-slept vest still counts
        # as ready: audio is what wakes it, so capture must keep running.
        self.vest_ready = lambda: True
        self.vest_gated = False

    def _snapshot(self):
        s = self.settings()
        for k in LEARN_SKIP:
            s.pop(k, None)
        return s

    def remember_app(self, app_key):
        """Store the current tuning under an app so it returns next session."""
        if not app_key:
            return
        self.learned[app_key.lower()] = self._snapshot()
        self._save_learned()

    def _save_learned(self):
        with contextlib.suppress(OSError):
            atomic_write_json(LEARNED_FILE, self.learned, indent=1)

    def settings(self):
        # persist the *preference* (start_enabled), never the runtime state:
        # shutdown and auto-follow toggle self.enabled, and writing that back
        # would come up muted after a restart
        return {"gain": self.gain, "floor": self.floor,
                "max": self.max_level, "stereo": self.stereo,
                "enabled": self.start_enabled, "source": self.source,
                "impact": self.impact, "impGain": self.impact_gain,
                "impMax": self.impact_max, "auto": self.auto_profiles,
                "spread": self.spread, "pad": self.pad_mirror,
                "agc": self.agc, "activity": self.activity,
                "surround": self.surround, "notify": self.notify}

    async def set_enabled(self, on, intent=False):
        if intent:
            self.start_enabled = bool(on)
        if on and not self.enabled:
            self.enabled = True
            self._task = asyncio.create_task(supervise("audio capture", self._run))
            log.info("audio mode ON (source %s)", self.source)
        elif not on and self.enabled:
            self.enabled = False
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._stop_proc()
            self._zero_levels()
            self.waiting = False
            log.info("audio mode OFF")

    def _zero_levels(self):
        self.level_l = self.level_r = self.rel = 0.0
        self.level_bl = self.level_br = 0.0
        self.impact_l = self.impact_r = 0.0
        self.impact_bl = self.impact_br = 0.0
        self.band_l = [0.0, 0.0, 0.0]
        self.band_r = [0.0, 0.0, 0.0]
        self.band_bl = [0.0, 0.0, 0.0]
        self.band_br = [0.0, 0.0, 0.0]

    def _stop_proc(self):
        proc, self._proc = self._proc, None
        if proc is not None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            with contextlib.suppress(RuntimeError):  # no running loop at exit
                asyncio.get_running_loop().create_task(proc.wait())  # reap it

    @staticmethod
    def _sink_id(name):
        for line in pactl("list", "short", "sinks").splitlines():
            parts = line.split()
            if len(parts) > 1 and parts[1] == name:
                return int(parts[0])
        return None

    def _ensure_surround_sink(self):
        """Virtual 5.1 sink + low-latency loopback to the real output.

        Games render true surround into it; we capture all 6 channels for
        positional haptics while the loopback keeps the audio audible. The
        modules live in PipeWire, so sound survives a daemon restart."""
        sid = self._sink_id(SUR_SINK)
        if sid is None:
            pactl("load-module", "module-null-sink",
                  f"sink_name={SUR_SINK}", "rate=48000",
                  f"channel_map={SUR_MAP}",
                  "sink_properties=device.description=Vest-5.1")
            sid = self._sink_id(SUR_SINK)
        if f"source={SUR_SINK}.monitor" not in pactl("list", "short", "modules"):
            pactl("load-module", "module-loopback",
                  f"source={SUR_SINK}.monitor", "latency_msec=20")
        return sid

    def teardown_surround(self):
        """Move streams back to the default sink and unload our modules."""
        sid = self._sink_id(SUR_SINK)
        if sid is not None:
            default = pactl("get-default-sink").strip()
            if default:
                for si in sink_inputs():
                    if si.get("sink") == sid:
                        pactl("move-sink-input", str(si["index"]), default)
        for line in pactl("list", "short", "modules").splitlines():
            if SUR_SINK in line:
                pactl("unload-module", line.split()[0])

    def _resolve_target(self):
        """Translate self.source into parec args; None if not available yet."""
        src = self.source
        if src == "auto":
            # games-only: the auto-follow loop binds an actual app when one
            # plays; until then capture nothing (silence, not system audio)
            return None
        if src.startswith("sink:"):
            return ["-d", src[5:] + ".monitor"]
        if src.startswith("appname:"):
            name = src[8:].lower()
            for si in sink_inputs():
                props = si.get("properties", {})
                app = (props.get("application.name")
                       or props.get("application.process.binary") or "")
                if app.lower() == name:
                    if self.surround:
                        sid = self._ensure_surround_sink()
                        if sid is not None and si.get("sink") != sid:
                            pactl("move-sink-input", str(si["index"]), SUR_SINK)
                        return ["-d", f"{SUR_SINK}.monitor"]
                    return [f"--monitor-stream={si['index']}"]
            return None
        if src.startswith("app:"):  # legacy raw index
            return [f"--monitor-stream={src[4:]}"]
        return ["-d", pactl("get-default-sink").strip() + ".monitor"]

    async def _run(self):
        """Capture supervisor: waits for the source, rebinds when streams die."""
        announced = False
        while True:
            if not self.vest_ready():
                # no vest to drive: don't burn CPU capturing and DSP-ing audio
                self.vest_gated = True
                self.waiting = True
                self._zero_levels()
                if not announced:
                    log.info("audio: waiting for the vest to connect")
                    announced = True
                await asyncio.sleep(1)
                continue
            self.vest_gated = False
            target = await asyncio.get_running_loop().run_in_executor(
                None, self._resolve_target)
            if target is None:
                if not announced:
                    what = "a game" if self.source == "auto" else self.source
                    log.info("audio: waiting for %s to play audio...", what)
                    announced = True
                self.level_l = self.level_r = self.rel = 0.0
                self.waiting = True
                await asyncio.sleep(3)
                continue
            announced = False
            self.waiting = False
            surround = self.surround
            chans = (["--channels=6", f"--channel-map={SUR_MAP}"] if surround
                     else ["--channels=2"])
            self._proc = await asyncio.create_subprocess_exec(
                "parec", "--format=s16le", f"--rate={AUDIO_RATE}", *chans,
                "--latency-msec=20", *target,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            log.info("audio capture started (%s%s)", " ".join(target),
                     " [5.1]" if surround else "")
            await self._pump(surround)
            self._stop_proc()
            await asyncio.sleep(1)

    async def _pump(self, surround=False):
        n_bands = 3  # sub (~25-75 Hz), bass (~75-125), lowmid (~125-275)
        nsig = 4 if surround else 2  # quadrant signals: FL, FR[, BL, BR]
        pairs = ((0, 1), (2, 3)) if surround else ((0, 1),)  # L/R per panel
        sm = [[0.0] * n_bands for _ in range(nsig)]
        peak = 1e-3
        hf_base = [0.0] * nsig
        hf_peak = 1e-3
        imp = [0.0] * nsig
        front_ema = rear_ema = 0.0
        def manual_thresh():
            return min(0.9, max(0.06, 0.9 - 0.09 * self.gain))

        def manual_ithresh():
            return min(0.65, max(0.05, 0.65 - 0.055 * self.impact_gain))

        # AGC (auto-scale) state: thresholds drift to hit an activity target
        # instead of tracking the sliders, so a loud mix stops buzzing
        # constantly and a quiet one still gets through. ~50 frames/s.
        auto_thresh = manual_thresh()
        auto_ithresh = manual_ithresh()
        duty = 0.0       # EMA of "vest is rumbling" (tau ~5 s)
        imp_rate = 0.0   # EMA of impact triggers per frame
        # rolling loudness estimates (tau ~10 s) let auto mode *guess* the
        # game's scale: they adapt the silence gate down for quiet games and
        # re-anchor the peak reference after e.g. a loud intro screen
        loud_ema = 0.0
        hf_ema = 0.0
        bin_hz = AUDIO_RATE / AUDIO_CHUNK  # 50 Hz per FFT bin
        band_edges = [(1, 2), (2, 3), (3, 6)]
        hf_lo, hf_hi = int(1000 / bin_hz), int(10000 / bin_hz)

        def bands(ch):
            spectrum = np.abs(np.fft.rfft(ch))
            norm = np.sqrt(AUDIO_CHUNK)
            lows = [float(np.sqrt(np.mean(spectrum[a:b] ** 2)) / norm)
                    for a, b in band_edges]
            hf = float(np.sqrt(np.mean(spectrum[hf_lo:hf_hi] ** 2)) / norm)
            return lows, hf

        def out(v, ref):
            rel = v / ref
            if rel <= self.thresh:
                return 0.0
            return ((rel - self.thresh) / (1 - self.thresh)) ** 1.5 * (
                self.max_level / 15.0
            )

        n_ch = 6 if surround else 2
        frame_bytes = AUDIO_CHUNK * 2 * n_ch
        buf = bytearray()
        try:
            while True:
                if not self.vest_ready():
                    log.info("audio: vest disconnected, pausing capture")
                    self._zero_levels()
                    return
                chunk = await self._proc.stdout.read(65536)
                if not chunk:
                    log.info("audio stream ended, will rebind")
                    self._zero_levels()
                    return
                buf += chunk
                if len(buf) < frame_bytes:
                    continue
                # keep only the newest complete frame: a stall would otherwise
                # leave stale audio queued forever, turning into permanent delay
                n = len(buf) // frame_bytes
                data = bytes(buf[(n - 1) * frame_bytes:n * frame_bytes])
                del buf[:n * frame_bytes]
                samples = (
                    np.frombuffer(data, dtype=np.int16)
                    .reshape(-1, n_ch).astype(np.float32) / 32768.0
                )
                if surround:
                    # center and LFE carry no direction: blend into the sides
                    fc = samples[:, 2] * 0.5
                    lfe = samples[:, 3] * 0.5
                    sigs = [samples[:, 0] + fc + lfe, samples[:, 1] + fc + lfe,
                            samples[:, 4] + lfe, samples[:, 5] + lfe]
                else:
                    sigs = [samples[:, 0], samples[:, 1]]
                res = [bands(s) for s in sigs]
                lows = [r[0] for r in res]
                hfs = [r[1] for r in res]
                if not self.stereo:
                    for a, b in pairs:
                        merged = [max(x, y) for x, y in zip(lows[a], lows[b])]
                        lows[a] = lows[b] = merged
                        hfs[a] = hfs[b] = max(hfs[a], hfs[b])
                if surround:
                    # stereo-only games leave the rear silent: detect that and
                    # let the mixer mirror the front instead of a dead back
                    front_ema = front_ema * 0.995 + 0.005 * (
                        max(lows[0]) + max(lows[1]) + hfs[0] + hfs[1])
                    rear_ema = rear_ema * 0.995 + 0.005 * (
                        max(lows[2]) + max(lows[3]) + hfs[2] + hfs[3])
                    self.rear_live = rear_ema > max(front_ema, 1e-5) * 0.02

                # auto mode lowers the silence gate toward the game's own
                # loudness so even quiet mixes register; manual keeps the knob
                gate = (min(self.floor, max(loud_ema * 0.15, 2e-4))
                        if self.agc else self.floor)

                # impacts: HF onsets (sword hits, gunshots) vs a slow baseline
                # so sustained noise (music, wind) never triggers
                for i in range(nsig):
                    imp[i] *= 0.55  # fast decay: sharp tap, not a drone
                if self.impact:
                    hf_now = max(hfs)
                    if hf_now > 2e-4:
                        hf_ema = (hf_now if hf_ema == 0.0
                                  else hf_ema * 0.998 + hf_now * 0.002)
                    hf_decay = (0.999 if self.agc and hf_peak > hf_ema * 4
                                else 0.99975)
                    hf_peak = max(hf_peak * hf_decay, *hfs)
                    ithresh = auto_ithresh if self.agc else manual_ithresh()
                    scale = self.impact_max / 15.0
                    trig = False
                    for i in range(nsig):
                        hf_base[i] = hf_base[i] * 0.985 + hfs[i] * 0.015
                        on = (hfs[i] - hf_base[i] * 1.5) / hf_peak
                        if hfs[i] > gate * 0.5 and on > ithresh:
                            imp[i] = max(imp[i], min(1.0, 0.3 + (on - ithresh) / (1 - ithresh)) * scale)
                            trig = True
                    # rate controller: aim for a taps-per-second budget so a
                    # drum-heavy mix can't machine-gun the chest, while quiet
                    # scenes drift back to full sensitivity
                    if self.agc and max(hfs) > gate * 0.5:
                        imp_rate = imp_rate * 0.996 + (0.004 if trig else 0.0)
                        tgt = 0.006 + 0.005 * self.activity
                        if imp_rate > tgt * 1.4:
                            auto_ithresh = min(0.85, auto_ithresh + 0.0015)
                        elif imp_rate < tgt * 0.6:
                            auto_ithresh = max(0.10, auto_ithresh - 0.0015)
                self.impact_l = imp[0] if imp[0] > 0.03 else 0.0
                self.impact_r = imp[1] if imp[1] > 0.03 else 0.0
                if surround:
                    self.impact_bl = imp[2] if imp[2] > 0.03 else 0.0
                    self.impact_br = imp[3] if imp[3] > 0.03 else 0.0

                # bass rumble: fast attack, slower release envelopes, per band
                for i in range(nsig):
                    sm[i] = [max(e, s * 0.90) for e, s in zip(lows[i], sm[i])]
                loud = max(v for s in sm for v in s)
                if loud > 2e-4:
                    loud_ema = (loud if loud_ema == 0.0
                                else loud_ema * 0.998 + loud * 0.002)
                self.thresh = auto_thresh if self.agc else manual_thresh()
                if loud < gate:
                    self.level_l = self.level_r = self.rel = 0.0
                    self.level_bl = self.level_br = 0.0
                    self.band_l = [0.0] * n_bands
                    self.band_r = [0.0] * n_bands
                    self.band_bl = [0.0] * n_bands
                    self.band_br = [0.0] * n_bands
                    continue
                # slow-decaying loudness reference: react to *relative* loudness
                # so only the louder beats thump instead of a constant buzz
                # re-anchor quickly when the reference is far above what the
                # game has been doing lately (loud intro -> quiet gameplay)
                decay = 0.999 if self.agc and peak > loud_ema * 4 else 0.99975
                peak = max(peak * decay, loud)
                self.rel = loud / peak

                outs = [[out(v, peak) for v in s] for s in sm]
                self.band_l, self.band_r = outs[0], outs[1]
                if surround:
                    self.band_bl, self.band_br = outs[2], outs[3]
                self.level_l = max(self.band_l)
                self.level_r = max(self.band_r)
                self.level_bl = max(self.band_bl)
                self.level_br = max(self.band_br)
                # duty controller: nudge the threshold until the fraction of
                # time the vest rumbles matches the activity target. Only runs
                # while audio is above the floor, so silence never drifts it.
                if self.agc:
                    act = max(self.level_l, self.level_r,
                              self.level_bl, self.level_br)
                    duty = duty * 0.996 + (0.004 if act > 0.01 else 0.0)
                    tgt = 0.04 + 0.036 * self.activity
                    if duty > tgt * 1.25:
                        auto_thresh = min(0.95, auto_thresh + 0.002)
                    elif duty < tgt * 0.75:
                        auto_thresh = max(0.20, auto_thresh - 0.002)
                    self.duty = duty
        except asyncio.CancelledError:
            self._zero_levels()
            raise
        except Exception as e:
            log.warning("audio capture error (will retry): %s", e)
            self._zero_levels()


def list_audio_apps():
    """Game-looking apps currently playing audio, newest stream first.

    Returns `[{"key", "name", "index", "binary"}]`: `key` is the lowercased
    name used for preset/learned matching, `name` the display name, `index`
    the PipeWire sink-input id (higher == started later) so the most recently
    launched app wins, and `binary` the process binary. An app is dropped when
    either its name *or* its process binary is a known non-game — a browser or
    Discord voice process can report a friendly `application.name` like
    "WEBRTC VoiceEngine" while running as `Discord`."""
    seen = {}
    for si in sink_inputs():
        props = si.get("properties", {})
        binary = (props.get("application.process.binary") or "")
        name = (props.get("application.name") or binary)
        if not name:
            continue
        key, bin_key = name.lower(), binary.lower()
        if key in NON_GAME_APPS or bin_key in NON_GAME_APPS:
            continue
        idx = int(si.get("index", 0) or 0)
        if key not in seen or idx > seen[key]["index"]:
            seen[key] = {"key": key, "name": name, "index": idx,
                         "binary": bin_key}
    return sorted(seen.values(), key=lambda a: a["index"], reverse=True)


# never auto-bind audio capture to these (browsers, comms, media players,
# our own test tone players) — matched against both application.name and
# application.process.binary
NON_GAME_APPS = {
    "brave", "firefox", "chromium", "chrome", "google chrome", "vivaldi",
    "opera", "microsoft edge", "discord", "vesktop", "webcord", "spotify",
    "webrtc voiceengine", "electron", "mpv", "vlc", "vlc media player",
    "telegram", "telegram desktop", "microsoft teams", "teams", "webex",
    "obs", "zoom", "slack", "signal", "easyeffects", "pavucontrol",
    "kodi", "steam", "steamwebhelper", "gamescope", "pacat", "paplay",
    "parec", "pw-play", "pw-cat", "pipewire", "wireplumber",
    "speech-dispatcher", "plasmashell", "gnome-shell", "kwin_wayland",
    "kded5", "kded6", "ksmserver", "plasma-browser-integration",
    "xdg-desktop-portal", "xdg-desktop-portal-gtk", "xdg-desktop-portal-kde",
    "steamvr", "vrwebhelper", "vrserver",
    "vrmonitor", "vrcompositor", "vrdashboard",
}

STEAM_ROOTS = (Path.home() / ".steam/steam",
               Path.home() / ".local/share/Steam",
               Path.home() / ".var/app/com.valvesoftware.Steam/data/Steam")
# Proton games report binary wine-preloader/wine64-preloader, not the exe
WINE_BINARIES = {"wine", "wine64", "wine-preloader", "wine64-preloader"}
_game_names_cache = (0.0, frozenset())


def installed_game_names():
    """Lowercased names + install dirs of installed Steam games (cached 5 min).

    Matching a stream's `application.name` against this is the strongest
    "this is a game" signal available without touching the game process."""
    global _game_names_cache
    stamp, names = _game_names_cache
    if stamp and time.monotonic() - stamp < 300:  # cache "no Steam" too
        return names
    found = set()
    for root in STEAM_ROOTS:
        libs = [str(root)]
        vdf = root / "steamapps" / "libraryfolders.vdf"
        with contextlib.suppress(OSError):
            libs += re.findall(r'"path"\s+"([^"]+)"',
                               vdf.read_text(errors="ignore"))
        for lib in libs:
            for mf in (Path(lib) / "steamapps").glob("appmanifest_*.acf"):
                with contextlib.suppress(OSError):
                    txt = mf.read_text(errors="ignore")
                    for pat in (r'"name"\s+"([^"]+)"',
                                r'"installdir"\s+"([^"]+)"'):
                        m = re.search(pat, txt)
                        if m:
                            found.add(m.group(1).lower())
    _game_names_cache = (time.monotonic(), frozenset(found))
    if found:
        log.info("detected %d installed Steam game names", len(found))
    return _game_names_cache[1]


def game_strength(entry, steam_names):
    """How game-like a stream looks: 2 = Steam game, 1 = Windows/Proton exe,
    0 = unknown (kept as a last resort so native games still auto-bind)."""
    key = entry["key"]
    base = key[:-4] if key.endswith(".exe") else key
    if base in steam_names or key in steam_names:
        return 2
    if key.endswith(".exe") or entry.get("binary", "") in WINE_BINARIES:
        return 1
    return 0


class PadMirror:
    """Mirror gamepad force-feedback to the vest.

    Grabs each FF-capable evdev gamepad and re-exposes it as a uinput clone;
    the game rumbles the clone, we forward the effect to the real pad (so
    controller rumble still works) and buzz the vest with the same envelope.
    No game involvement at all — works for anything using evdev/SDL rumble.
    """

    def __init__(self, state):
        self.state = state
        self.enabled = False
        self.tasks = {}  # dev path -> asyncio task
        self.names = {}  # dev path -> device name
        self._scan_task = None

    async def set_enabled(self, on):
        if on and not self.enabled:
            if evdev is None:
                log.warning("pad mirror: python-evdev not installed")
                return
            self.enabled = True
            self._scan_task = asyncio.create_task(self._scan())
            log.info("pad mirror ON")
        elif not on and self.enabled:
            self.enabled = False
            self._scan_task.cancel()
            for t in self.tasks.values():
                t.cancel()
            self.tasks.clear()
            self.names.clear()
            self.state.active.pop("pad", None)
            log.info("pad mirror OFF")

    async def _scan(self):
        # Poll fast: Steam Input's virtual gamepad only appears at game launch
        # and the game binds it within ms, so a slow scan misses the window.
        # cooldown is keyed by (path, inode): it stops a retry loop on the
        # *same* node, but a node Steam re-created at the same path (new
        # inode) must be grabbed at once — waiting lets the game bind it first
        cooldown = {}  # (path, inode) -> monotonic time its last mirror task ended
        # probe each node once; inode changes when a device is re-created at
        # the same path, so genuinely new hardware still gets looked at
        rejected = set()  # (path, inode) of nodes without rumble
        inodes = {}  # dev path -> inode of the node being mirrored
        while True:
            for path, t in list(self.tasks.items()):
                if t.done():
                    self.tasks.pop(path)
                    self.names.pop(path, None)
                    cooldown[(path, inodes.pop(path, None))] = time.monotonic()
            for path in evdev.list_devices():
                if path in self.tasks:
                    continue
                try:
                    ino = os.stat(path).st_ino
                except OSError:
                    continue
                if (path, ino) in rejected:
                    continue
                last = cooldown.get((path, ino))
                if last is not None and time.monotonic() - last < 10:
                    continue
                try:
                    dev = evdev.InputDevice(path)
                    caps = dev.capabilities()
                    if (EC.EV_FF not in caps or EC.FF_RUMBLE not in caps[EC.EV_FF]
                            or "(vest)" in (dev.name or "")):
                        dev.close()
                        rejected.add((path, ino))
                        continue
                except OSError:
                    continue
                self.names[path] = dev.name
                inodes[path] = ino
                self.tasks[path] = asyncio.create_task(self._mirror(path, dev))
                log.info("pad mirror: attached to %s (%s)", dev.name, path)
            await asyncio.sleep(0.5)

    async def _mirror(self, path, dev):
        ui = None
        loop = asyncio.get_running_loop()
        phys_ids = {}    # clone effect id -> physical effect id
        magnitudes = {}  # clone effect id -> (amp 0..1, length_ms)
        try:
            dev.grab()
            caps = dev.capabilities(absinfo=True)
            caps.pop(EC.EV_SYN, None)
            ui = evdev.UInput(caps, name=f"{dev.name} (vest)",
                              vendor=dev.info.vendor, product=dev.info.product,
                              version=dev.info.version, bustype=dev.info.bustype)

            def effect_amp(eff):
                try:
                    if eff.type == EC.FF_RUMBLE:
                        r = eff.u.ff_rumble_effect
                        return max(r.strong_magnitude, r.weak_magnitude) / 0xFFFF
                    if eff.type == EC.FF_PERIODIC:
                        return abs(eff.u.ff_periodic_effect.magnitude) / 0x7FFF
                except Exception:
                    pass
                return 0.5

            def on_clone_readable():
                while True:
                    ev = ui.read_one()
                    if ev is None:
                        return
                    if ev.type == EC.EV_UINPUT and ev.code == EC.UI_FF_UPLOAD:
                        upload = ui.begin_upload(ev.value)
                        eff = upload.effect
                        local_id = eff.id
                        length = getattr(eff.replay, "length", 300) or 300
                        magnitudes[local_id] = (effect_amp(eff), int(length))
                        with contextlib.suppress(Exception):
                            eff.id = phys_ids.get(local_id, -1)
                            phys_ids[local_id] = dev.upload_effect(eff)
                        upload.retval = 0
                        ui.end_upload(upload)
                    elif ev.type == EC.EV_UINPUT and ev.code == EC.UI_FF_ERASE:
                        erase = ui.begin_erase(ev.value)
                        pid = phys_ids.pop(erase.effect_id, None)
                        if pid is not None:
                            with contextlib.suppress(Exception):
                                dev.erase_effect(pid)
                        magnitudes.pop(erase.effect_id, None)
                        erase.retval = 0
                        ui.end_erase(erase)
                    elif ev.type == EC.EV_FF:
                        pid = phys_ids.get(ev.code)
                        if pid is not None:
                            with contextlib.suppress(Exception):
                                dev.write(EC.EV_FF, pid, ev.value)
                        amp, ms = magnitudes.get(ev.code, (0.5, 300))
                        if ev.value > 0:
                            self.state.pad_rumble(amp, ms)
                        else:
                            self.state.active.pop("pad", None)

            loop.add_reader(ui.fd, on_clone_readable)
            try:
                async for ev in dev.async_read_loop():
                    ui.write_event(ev)
            finally:
                loop.remove_reader(ui.fd)
        except asyncio.CancelledError:
            pass
        except OSError as e:
            if e.errno == errno.ENODEV:
                # normal: the pad was unplugged, or Steam Input tore down its
                # virtual pad (it re-creates them on game/config changes)
                log.info("pad mirror: %s went away", path)
            elif e.errno in (errno.EACCES, errno.EPERM):
                log.warning("pad mirror %s failed: %s (need /dev/uinput access?)", path, e)
            else:
                log.warning("pad mirror %s failed: %s", path, e)
        finally:
            with contextlib.suppress(Exception):
                dev.ungrab()
            with contextlib.suppress(Exception):
                dev.close()
            if ui is not None:
                with contextlib.suppress(Exception):
                    ui.close()
            log.info("pad mirror: detached from %s", path)


SDK2_CACHE = STATE_DIR / "sdk2_cache"
SDK2_DEFS_URL = (
    "https://sdk-apis.bhaptics.com/api/v1/haptic-definitions/workspace/latest"
    "?workspace-id={wid}&api-key={key}&last-version=0"
)


def find_project(d, depth=0):
    """Find a .tact-style dict (has Tracks/tracks) nested anywhere inside d."""
    if depth > 8:
        return None
    if isinstance(d, dict):
        if "Tracks" in d or "tracks" in d:
            return d
        for v in d.values():
            if isinstance(v, str) and '"racks"' in v.lower().replace("t", ""):
                with contextlib.suppress(ValueError):
                    v = json.loads(v)
            r = find_project(v, depth + 1)
            if r is not None:
                return r
    elif isinstance(d, list):
        for v in d:
            r = find_project(v, depth + 1)
            if r is not None:
                return r
    return None


def extract_events(data, found=None, depth=0):
    """Best-effort walk of an SDK2 definitions bundle: eventName -> project."""
    if found is None:
        found = {}
    if depth > 10:
        return found
    if isinstance(data, str) and len(data) > 2 and data.lstrip()[:1] in "[{":
        with contextlib.suppress(ValueError):
            data = json.loads(data)
    if isinstance(data, dict):
        name = None
        for k in ("eventName", "EventName", "key", "Key", "eventId", "name"):
            v = data.get(k)
            if isinstance(v, str) and v:
                name = v
                break
        if name:
            project = find_project(data)
            if project is None and "audioFilePatterns" in data:
                project = clip_project(data)
            if project is not None:
                found.setdefault(name, project)
        for v in data.values():
            extract_events(v, found, depth + 1)
    elif isinstance(data, list):
        for v in data:
            extract_events(v, found, depth + 1)
    return found


def fetch_sdk2_definitions(workspace_id, api_key):
    """Blocking cloud fetch (run in executor). Returns parsed JSON or None."""
    import urllib.request

    url = SDK2_DEFS_URL.format(wid=workspace_id, key=api_key)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        return data
    except Exception as e:
        log.warning("sdk2 cloud fetch failed for %r: %s", workspace_id, e)
        return None


SDK2_CACHE_RE = re.compile(r"^(auth|cloud)-([0-9A-Za-z]+?)(?:-\d{8}-\d{6})?\.json$")


def sdk2_cache_workspace(filename):
    m = SDK2_CACHE_RE.match(filename)
    return m.group(2) if m else ""


def _store_and_extract_sdk2(data, source):
    """Cache one raw bundle per game (rewritten only when it changed) and
    parse its events. Runs in an executor thread."""
    text = data if isinstance(data, str) else json.dumps(data)
    with contextlib.suppress(OSError):
        SDK2_CACHE.mkdir(parents=True, exist_ok=True)
        path = SDK2_CACHE / f"{source}.json"
        try:
            same = path.read_text() == text
        except OSError:
            same = False
        if not same:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(text)
            tmp.replace(path)
    return extract_events(data)


def dedupe_sdk2_cache():
    """One-time cleanup of the old per-connect dumps (auth-<ws>-<stamp>.json):
    keep the newest per game as auth-<ws>.json, drop byte-identical copies."""
    groups = {}
    for f in SDK2_CACHE.glob("*-*.json"):
        m = SDK2_CACHE_RE.match(f.name)
        if m and f.stem != f"{m.group(1)}-{m.group(2)}":
            groups.setdefault(f"{m.group(1)}-{m.group(2)}", []).append(f)
    removed = 0
    for source, files in groups.items():
        files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        keep = SDK2_CACHE / f"{source}.json"
        with contextlib.suppress(OSError):
            kept = {keep.read_bytes()} if keep.exists() else set()
            if not kept:
                files[0].replace(keep)
                kept.add(keep.read_bytes())
                files = files[1:]
            for f in files:
                if f.read_bytes() in kept:  # only exact duplicates are dropped
                    f.unlink()
                    removed += 1
    if removed:
        log.info("sdk2 cache: removed %d duplicate dumps", removed)


class PlayerState:
    def __init__(self, vest):
        self.vest = vest
        self.mapping = VestMapping()
        self.feel = Feel()
        self.audio = AudioEngine()
        self.audio.vest_ready = lambda: vest.connected or vest.idle
        self.registered = {}  # key -> raw project dict
        self.active = {}  # key -> Effect
        self.clients = set()
        self.meter_subs = set()
        self.game_clients = {}  # ws -> display name (non-UI SDK1 + SDK2 clients)
        self.last_motors = [0] * 40
        self.presets = {}
        self.active_preset = ""
        if PRESETS_FILE.exists():
            with contextlib.suppress(Exception):
                self.presets = json.loads(PRESETS_FILE.read_text())
        self.effects = {}
        if EFFECTS_FILE.exists():
            with contextlib.suppress(Exception):
                self.effects = json.loads(EFFECTS_FILE.read_text())
        self.osc_front = [0.0] * 20
        self.osc_back = [0.0] * 20
        # -inf, not 0: right after boot monotonic() is near 0 and 0.0 would
        # read as "OSC seen seconds ago", muting audio mode for no reason
        self.osc_last = float("-inf")
        # a game client only pauses audio mode while it is actually driving the
        # vest — a mod that connects but never sends patterns must not mute it
        self.game_active_until = float("-inf")
        self.sdk2_events = {}  # eventName -> project dict (all games + imports)
        # workspace id -> {eventName -> project}: games reuse names like
        # "death", so a connection looks up its own game's events first
        self.sdk2_ws_events = {}
        self.auto_profile_app = ""  # display name while an auto profile is active
        # auto-follow session: {"app", "preset", "saved", "saved_preset", "start"}
        # — lives here (not in the loop) so UI/preset handlers and config
        # persistence can see that a game is currently bound
        self.auto_session = None
        self.pad = PadMirror(self)
        self._surtest = None
        self.patterns = set()  # keys loaded from patterns/*.tact (persisted)
        self.missing_keys = {}  # names games asked for but we don't have
        self.import_note = ""
        self._load_sdk2_cache()
        self._load_patterns()  # after cache: explicit imports win over cache
        self.relay_host = False
        self.host_remote = ""
        self.relay_error = ""

    def add_pattern(self, name, project, save=True):
        """Register a pattern under both SDK1 (submit-by-key) and SDK2
        (SdkPlay event name), optionally persisting it to patterns/."""
        if save:
            PATTERNS_DIR.mkdir(parents=True, exist_ok=True)
            (PATTERNS_DIR / f"{name}.tact").write_text(json.dumps(project))
        self.registered[name] = project
        self.patterns.add(name)
        self.sdk2_events[name] = project
        self.missing_keys.pop(name, None)

    def _note_missing(self, name):
        if not name:
            return
        self.missing_keys[str(name)] = time.time()
        while len(self.missing_keys) > 30:
            self.missing_keys.pop(next(iter(self.missing_keys)))

    def _load_patterns(self):
        """Pre-register patterns/*.tact so mods that submit by key without
        registering first (expecting Player-installed defaults) still work."""
        for f in sorted(PATTERNS_DIR.glob("*.tact")):
            try:
                project = find_project(json.loads(f.read_text()))
            except Exception as e:
                log.warning("patterns: skipping %s: %s", f.name, e)
                continue
            if project is None:
                log.warning("patterns: no Tracks found in %s", f.name)
                continue
            self.add_pattern(f.stem, project, save=False)
        if self.patterns:
            log.info("patterns: %d loaded from %s", len(self.patterns), PATTERNS_DIR)

    def _load_sdk2_cache(self):
        """Relearn SDK2 event definitions from previous sessions."""
        dedupe_sdk2_cache()
        for f in sorted(SDK2_CACHE.glob("*.json")):
            with contextlib.suppress(Exception):
                events = extract_events(json.loads(f.read_text()))
                self._learn_sdk2(events, sdk2_cache_workspace(f.name))
        if self.sdk2_events:
            log.info("sdk2: %d events restored from cache", len(self.sdk2_events))

    def _learn_sdk2(self, events, workspace_id):
        self.sdk2_events.update(events)
        if workspace_id:
            self.sdk2_ws_events.setdefault(workspace_id, {}).update(events)

    def sdk2_event(self, name, workspace_id=""):
        """Project for an SDK2 event: user imports win, then the calling
        game's own definitions, then any game's."""
        if name not in self.patterns:
            project = self.sdk2_ws_events.get(workspace_id, {}).get(name)
            if project is not None:
                return project
        return self.sdk2_events.get(name)

    @property
    def osc_active(self):
        return (time.monotonic() - self.osc_last) < 5.0

    @property
    def audio_suppressed(self):
        return (time.monotonic() < self.game_active_until) or self.osc_active

    def touch_game(self):
        """Mark a game SDK client as actively driving the vest; audio mode
        yields to it only for a short window after each real pattern."""
        self.game_active_until = time.monotonic() + GAME_ACTIVE_WINDOW

    def notify(self, title, body, urgency="normal", icon="input-gaming", replace=None):
        if self.audio.notify:
            notify(title, body, urgency, icon, replace)

    def list_audio_sources(self):
        out = [{"value": "auto", "label": "Games only — auto (recommended)"},
               {"value": "default", "label": "System output (all audio)"}]
        try:
            sinks = json.loads(pactl("-f", "json", "list", "sinks"))
            for s in sinks:
                out.append({"value": f"sink:{s['name']}",
                            "label": f"Output: {s.get('description', s['name'])}"})
            inputs = sink_inputs()
            seen = set()
            for si in inputs:
                props = si.get("properties", {})
                app = props.get("application.name") or props.get("application.process.binary", "?")
                if app.lower() in seen:
                    continue
                seen.add(app.lower())
                media = props.get("media.name", "")
                label = f"App: {app}" + (f" — {media[:40]}" if media and media != app else "")
                out.append({"value": f"appname:{app}", "label": label})
        except Exception as e:
            log.warning("audio source listing failed: %s", e)
        return out

    def status_message(self):
        return json.dumps(
            {
                "RegisteredKeys": list(self.registered.keys()),
                "ActiveKeys": list(self.active.keys()),
                "ConnectedDeviceCount": 1 if self.vest.connected else 0,
                "ConnectedPositions": ["Vest"] if self.vest.connected else [],
                "VestIdle": bool(self.vest.idle),
                "RemotePlay": remote_play_enabled(),
                "RelayHost": self.relay_host,
                "RelayHostRemote": self.host_remote,
                "RelayHostError": self.relay_error,
                "Mapping": self.mapping.as_dict(),
                "Feel": self.feel.as_dict(),
                "AudioMode": self.audio.enabled,
                "AudioStereo": self.audio.stereo,
                "AudioGain": self.audio.gain,
                "AudioMax": self.audio.max_level,
                "AudioImpact": self.audio.impact,
                "AudioImpGain": self.audio.impact_gain,
                "AudioImpMax": self.audio.impact_max,
                "AudioSuppressed": self.audio_suppressed,
                "AudioSource": self.audio.source,
                "AudioAuto": self.audio.auto_profiles,
                "AudioSpread": self.audio.spread,
                "AudioAgc": self.audio.agc,
                "AudioActivity": self.audio.activity,
                "AudioSurround": self.audio.surround,
                "AudioNotify": self.audio.notify,
                "LearnedApps": sorted(self.audio.learned),
                "AutoProfileApp": self.auto_profile_app,
                "PadMirror": self.audio.pad_mirror,
                "PadDevices": sorted(self.pad.names.values()),
                "Presets": sorted(self.presets.keys()),
                "ActivePreset": self.active_preset,
                "GameClients": sorted(set(self.game_clients.values()))
                + (["VRChat (OSC)"] if self.osc_active else []),
                "Effects": sorted(k for k in self.effects if not k.startswith("__")),
                "Patterns": sorted(self.patterns),
                "Sdk2Events": sorted(self.sdk2_events),
                "MissingKeys": sorted(self.missing_keys),
                "Battery": self.vest.battery,
                "Version": __version__,
            }
        )

    def _drop_game_clients(self):
        for ws in list(self.game_clients):
            with contextlib.suppress(Exception):
                asyncio.create_task(ws.close())

    def start_relay_host(self):
        if self.relay_host:
            return
        self.relay_host = True
        self.relay_error = ""
        asyncio.create_task(self._find_vest_host())
        self._drop_game_clients()

    async def _find_vest_host(self):
        while self.relay_host and not self.host_remote:
            remote = remote_host_from_config() or await discover_vest_host()
            if not self.relay_host:
                return
            if remote:
                self.host_remote = remote
                self.relay_error = ""
                log.info("relay host: forwarding haptics to %s", remote)
                self._drop_game_clients()
            else:
                self.relay_error = "no vest device found on the LAN — enable Remote Play there"
                await asyncio.sleep(3)

    async def wait_for_remote(self, timeout=4.0):
        deadline = time.monotonic() + timeout
        while self.relay_host and not self.host_remote and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        return bool(self.host_remote)

    def stop_relay_host(self):
        self.relay_host = False
        self.host_remote = ""
        self.relay_error = ""

    async def proxy_sdk1(self, ws):
        log.info("relay host: proxying SDK1 %s -> %s", ws.request.path, self.host_remote)
        uri = "ws://%s:%d%s" % (self.host_remote, SDK1_PORT, ws.request.path)
        async with websockets.connect(uri, max_size=16 * 2**20) as up:
            await self._pump(ws, up)

    async def proxy_sdk2(self, ws):
        log.info("relay host: proxying SDK2 %s -> %s", ws.request.path, self.host_remote)
        uri = "wss://%s:%d%s" % (self.host_remote, SDK2_PORT, ws.request.path)
        async with websockets.connect(uri, ssl=sdk2_client_ssl_context(),
                                     max_size=16 * 2**20) as up:
            await self._pump(ws, up)

    @staticmethod
    async def _pump(a, b):
        async def one(src, dst):
            try:
                async for msg in src:
                    await dst.send(msg)
            except Exception:
                pass
        tasks = [asyncio.create_task(one(a, b)), asyncio.create_task(one(b, a))]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def handle(self, payload, game=False):
        for reg in payload.get("Register") or []:
            key = reg.get("Key")
            project = reg.get("Project")
            if key and project:
                self.registered[key] = project
                self.missing_keys.pop(key, None)
                log.info("registered %r", key)

        for sub in payload.get("Submit") or []:
            stype = sub.get("Type")
            key = sub.get("Key", "")
            if stype == "turnOffAll":
                self.active.clear()
            elif stype == "turnOff":
                self.active.pop(key, None)
            elif stype == "frame":
                timeline = compile_frame_submit(sub.get("Frame") or {})
                if timeline:
                    self.active[key or f"frame{time.monotonic()}"] = Effect(key, timeline)
                    if game:
                        self.touch_game()
            elif stype == "raw":
                timeline = compile_raw_submit(sub.get("Frame") or {})
                if timeline:
                    self.active[key or "raw"] = Effect(key, timeline)
                    if game:
                        self.touch_game()
            elif stype == "key":
                params = sub.get("Parameters") or {}
                project = self.registered.get(params.get("altKey") or key)
                if project is None:
                    log.warning("submit for unregistered key %r", key)
                    self._note_missing(params.get("altKey") or key)
                    continue
                timeline = compile_project(
                    project,
                    intensity_ratio=float(params.get("intensityRatio", 1.0)),
                    duration_ratio=float(params.get("durationRatio", 1.0)),
                )
                if timeline:
                    self.active[key] = Effect(key, timeline)
                    if game:
                        self.touch_game()

        if isinstance(payload.get("Feel"), dict):
            self.feel.update(payload["Feel"])
            log.info("feel: %s", self.feel.as_dict())

        if payload.get("Wake"):
            self.vest.mark_active()

        if payload.get("FeelTest"):
            self.active["feeltest"] = Effect("feeltest", feel_test_timeline())

        if "SetMapping" in payload:
            try:
                m = payload["SetMapping"]
                self.mapping.set(m["front"], m["back"])
            except Exception as e:
                log.warning("bad SetMapping: %s", e)

        if "AudioMode" in payload:
            am = payload["AudioMode"]
            if isinstance(am, dict):
                am = dict(am)
                if self.auto_session and am.get("source") == "auto":
                    # "arm for games" while a game is bound: already in auto,
                    # don't unbind the game that's playing
                    am.pop("source")
                await self.apply_audio_settings(am)
                # tuning a game's sliders keeps its session; changing the
                # source (or tuning outside a session) leaves the preset
                tuned = any(k not in GLOBAL_KEYS for k in am)
                if "source" in am or (tuned and not self.auto_session):
                    self.active_preset = ""
                enabled = bool(am.get("enabled", self.audio.enabled))
            else:
                enabled = bool(am)
            await self.audio.set_enabled(enabled, intent=True)

        if "PadMirror" in payload:
            self.audio.pad_mirror = bool(payload["PadMirror"])
            await self.pad.set_enabled(self.audio.pad_mirror)

        if "RemotePlay" in payload:
            want = bool(payload["RemotePlay"])
            if want != remote_play_enabled():
                if want:
                    self.stop_relay_host()
                set_remote_play(want)
                asyncio.get_running_loop().call_later(0.4, _rebind)

        if "RelayHost" in payload:
            if payload["RelayHost"]:
                self.start_relay_host()
            else:
                self.stop_relay_host()

        if (payload.get("SurroundTest")
                and (self._surtest is None or self._surtest.done())):
            self._surtest = asyncio.create_task(self.surround_test())

        if "SavePreset" in payload:
            name = str(payload["SavePreset"]).strip()
            if name:
                snap = strip_global(self.audio.settings())
                self.presets[name] = snap
                self._save_presets()
                self.active_preset = name
                sess = self.auto_session
                if sess and name.lower() == sess["app"]:
                    # "save this game as a preset": the session continues
                    # under the preset instead of reading as a user override
                    sess["preset"] = name
                log.info("preset saved: %r", name)

        if "ApplyPreset" in payload:
            name = str(payload["ApplyPreset"])
            preset = self.presets.get(name)
            if preset:
                await self.apply_audio_settings(strip_global(preset))
                self.active_preset = name
                log.info("preset applied: %r", name)

        if "DeletePreset" in payload:
            name = str(payload["DeletePreset"])
            if self.presets.pop(name, None) is not None:
                self._save_presets()
                if self.active_preset == name:
                    self.active_preset = ""
                log.info("preset deleted: %r", name)

        if payload.get("ForgetLearned"):
            n = len(self.audio.learned)
            self.audio.learned.clear()
            self.audio._save_learned()
            log.info("auto-learned settings cleared (%d apps)", n)

        if "SaveEffect" in payload:
            fx = payload["SaveEffect"]
            name = str(fx.get("name", "")).strip()
            frames = []
            for f in fx.get("frames", [])[:200]:
                frames.append({
                    "ms": max(TICK_MS, min(10000, int(f.get("ms", 100)))),
                    "front": [min(1.0, max(0.0, float(v))) for v in (f.get("front") or [])][:20],
                    "back": [min(1.0, max(0.0, float(v))) for v in (f.get("back") or [])][:20],
                })
            if name and frames:
                self.effects[name] = frames
                self._save_effects()
                if not name.startswith("__"):
                    log.info("effect saved: %r (%d frames)", name, len(frames))

        if "PlayEffect" in payload:
            name = str(payload["PlayEffect"])
            frames = self.effects.get(name)
            if frames:
                timeline = []
                for f in frames:
                    fr = (f["front"] + [0.0] * 20)[:20]
                    bk = (f["back"] + [0.0] * 20)[:20]
                    timeline += [("mapped", fr, bk)] * max(1, f["ms"] // TICK_MS)
                self.active[f"fx:{name}"] = Effect(name, timeline)

        if ("DeleteEffect" in payload
                and self.effects.pop(str(payload["DeleteEffect"]), None) is not None):
            self._save_effects()

        if "ImportTact" in payload:
            it = payload["ImportTact"] or {}
            # strict name filter: it becomes a filename
            clean = lambda n: re.sub(r"[^\w\- .]", "", str(n)).strip(". ")
            name = clean(it.get("name", ""))
            data = it.get("project")
            # root-level Tracks = plain .tact; otherwise prefer the manifest
            # parser (find_project would greedily grab the first embedded
            # pattern of a multi-event manifest)
            project = None
            if isinstance(data, dict) and ("Tracks" in data or "tracks" in data):
                project = data
            elif not extract_events(data):
                project = find_project(data)
            if name and project is not None:
                self.add_pattern(name, project)
                self.import_note = f"imported pattern “{name}”"
                log.info("pattern imported: %r", name)
            else:
                # not a single .tact — maybe an SDK2 definitions manifest
                # (Unity/Unreal mods ship one JSON with many named events)
                added = []
                for ev, proj in extract_events(data).items():
                    en = clean(ev)
                    if en:
                        self.add_pattern(en, proj)
                        added.append(en)
                if added:
                    self.import_note = (
                        f"“{name or 'manifest'}”: imported {len(added)} events — "
                        + ", ".join(added[:8])
                        + ("…" if len(added) > 8 else ""))
                    log.info("manifest imported: %d events from %r", len(added), name)
                else:
                    self.import_note = f"“{name or 'file'}”: no haptic patterns found"
                    log.info("ImportTact: nothing usable in %r", name)

        if "DeletePattern" in payload:
            name = str(payload["DeletePattern"])
            if name in self.patterns:
                self.patterns.discard(name)
                self.registered.pop(name, None)
                self.sdk2_events.pop(name, None)
                with contextlib.suppress(OSError):
                    (PATTERNS_DIR / f"{name}.tact").unlink()
                log.info("pattern deleted: %r", name)

        if "PlayEvent" in payload:
            name = str(payload["PlayEvent"])
            project = self.sdk2_events.get(name)
            timeline = compile_event(project) if project is not None else []
            if timeline:
                self.active[f"sdk2ui:{name}"] = Effect(name, timeline)

    def _save_effects(self):
        with contextlib.suppress(OSError):
            atomic_write_json(EFFECTS_FILE,
                              {k: v for k, v in self.effects.items() if not k.startswith("__")})

    async def apply_audio_settings(self, d):
        a = self.audio
        a.gain = float(d.get("gain", a.gain))
        a.floor = float(d.get("floor", a.floor))
        a.max_level = int(d.get("max", a.max_level))
        a.stereo = bool(d.get("stereo", a.stereo))
        a.impact = bool(d.get("impact", a.impact))
        a.impact_gain = float(d.get("impGain", a.impact_gain))
        a.impact_max = int(d.get("impMax", a.impact_max))
        a.auto_profiles = bool(d.get("auto", a.auto_profiles))
        a.spread = bool(d.get("spread", a.spread))
        a.agc = bool(d.get("agc", a.agc))
        a.notify = bool(d.get("notify", a.notify))
        a.activity = min(10.0, max(1.0, float(d.get("activity", a.activity))))
        new_src = str(d.get("source", a.source))
        new_sur = bool(d.get("surround", a.surround))
        if new_src != a.source or new_sur != a.surround:
            was_enabled = a.enabled
            await a.set_enabled(False)
            if a.surround and not new_sur:
                await asyncio.get_running_loop().run_in_executor(
                    None, a.teardown_surround)
            a.source, a.surround = new_src, new_sur
            if was_enabled:
                await a.set_enabled(True)
        # binding audio (auto or manual) is intent to use the vest: wake it
        # even though capture is gated while it sleeps
        self.vest.mark_active()

    def _save_presets(self):
        with contextlib.suppress(OSError):
            atomic_write_json(PRESETS_FILE, self.presets, indent=1)

    async def ingest_sdk2_defs(self, data, source, workspace_id):
        if data is None:
            return
        # bundles run to megabytes: parse and write off the event loop so
        # the mixer keeps its 20 ms tick while a game connects
        events = await asyncio.get_running_loop().run_in_executor(
            None, _store_and_extract_sdk2, data, source)
        if events:
            self._learn_sdk2(events, workspace_id)
            log.info("sdk2: learned %d events from %s: %s",
                     len(events), source, list(events)[:10])
        else:
            log.warning("sdk2: no events parsed from %s (raw saved to sdk2_cache/)", source)

    async def handle_sdk2(self, ws, payload, workspace_id, api_key):
        mtype = g(payload, "Type", "") or ""
        raw_inner = g(payload, "Message")
        inner = {}
        if isinstance(raw_inner, str) and raw_inner:
            with contextlib.suppress(ValueError):
                inner = json.loads(raw_inner)
        elif isinstance(raw_inner, dict):
            inner = raw_inner

        async def reply(t, obj):
            await ws.send(json.dumps(
                {"Type": t, "Message": obj if isinstance(obj, str) else json.dumps(obj)}
            ))

        async def send_devices():
            dev = {
                "position": 0, "deviceName": "TactSuitPro",
                "address": "", "connected": self.vest.connected,
                "battery": 100, "audioJackIn": False, "paired": True, "vsm": 0,
            }
            await reply("ServerDevices", [dev])

        if mtype in ("SdkRequestAuth", "SdkRequestAuthInit", "SdkRequestReInit"):
            if g(inner, "Haptic") is not None:
                await self.ingest_sdk2_defs(g(inner, "Haptic"), f"auth-{workspace_id}",
                                            workspace_id)
            # fetch when *this* game is unknown — events cached from other
            # games used to block the fetch for every new game
            if workspace_id and not self.sdk2_ws_events.get(workspace_id):
                data = await asyncio.get_running_loop().run_in_executor(
                    None, fetch_sdk2_definitions, workspace_id, api_key
                )
                if data:
                    await self.ingest_sdk2_defs(g(data, "Message", data),
                                                f"cloud-{workspace_id}", workspace_id)
            await reply("ServerReady", "")
            await send_devices()
            events = self.sdk2_ws_events.get(workspace_id) or self.sdk2_events
            event_list = [
                {"eventName": name, "eventTime": event_duration_ms(p) if p else 1000}
                for name, p in events.items()
            ]
            await reply("ServerEventList", event_list)

        elif mtype in ("SdkPing", "SdkPingAll"):
            await send_devices()

        elif mtype == "SdkPlayDotMode":
            values = g(inner, "MotorValues") or []
            duration = int(g(inner, "DurationMillis", 1000))
            req_id = g(inner, "RequestId", 0)
            if int(g(inner, "Position", 0)) == 0:  # 0 = vest
                vals = [norm_intensity(v) for v in values][:40]
                vals += [0.0] * (40 - len(vals))
                ticks = max(1, duration // TICK_MS)
                timeline = [("mapped", vals[:20], vals[20:])] * ticks
                self.active[f"sdk2:{req_id}"] = Effect("dot", timeline)
                self.touch_game()

        elif mtype == "SdkPlay":
            name = g(inner, "EventName", "")
            req_id = g(inner, "RequestId", 0)
            intensity = float(g(inner, "Intensity", 1.0))
            duration = float(g(inner, "Duration", 1.0)) or 1.0
            project = self.sdk2_event(name, workspace_id)
            timeline = []
            if project is not None:
                timeline = compile_event(
                    project, intensity_ratio=intensity, duration_ratio=duration
                )
            if not timeline:
                # unknown event: generic 250ms whole-vest pulse so the game
                # still gives feedback; raw definition (if any) is in sdk2_cache/
                log.info("sdk2: fallback pulse for event %r", name)
                self._note_missing(name)
                lvl = min(1.0, 0.45 * intensity)
                ticks = max(1, int(250 * duration) // TICK_MS)
                timeline = [("mapped", [lvl] * 20, [lvl] * 20)] * ticks
            self.active[f"sdk2:{req_id}"] = Effect(name, timeline)
            self.touch_game()

        elif mtype == "SdkStopAll":
            for k in [k for k in self.active if k.startswith("sdk2:")]:
                self.active.pop(k, None)
        elif mtype == "SdkStopByRequestId":
            self.active.pop(f"sdk2:{g(inner, 'RequestId', 0)}", None)
        elif mtype == "SdkStopByEventId":
            name = g(inner, "EventId", g(inner, "EventName", ""))
            for k, eff in list(self.active.items()):
                if k.startswith("sdk2:") and eff.key == name:
                    self.active.pop(k, None)
        else:
            log.info("sdk2: unhandled message type %r, raw: %.300s",
                     mtype, json.dumps(payload))

    async def surround_test(self):
        """Play a 5.1 quadrant sweep into the virtual sink so positional
        haptics can be verified without configuring any game."""
        a = self.audio
        await asyncio.get_running_loop().run_in_executor(None, a._ensure_surround_sink)
        # already capturing vest51 (game routed through it)? just mix in
        bound = (a.enabled and a.surround and not a.waiting
                 and (a.source.startswith("appname:")
                      or a.source == f"sink:{SUR_SINK}"))
        stash = None
        if not bound:
            stash = (a.source, a.surround, a.enabled)
            await self.apply_audio_settings(
                {"source": f"sink:{SUR_SINK}", "surround": True})
            if not a.enabled:
                await a.set_enabled(True)
            await asyncio.sleep(1.0)  # let the capture bind before the sweep
        try:
            await self._play_sweep()
        finally:
            if stash is not None:
                src, sur, en = stash
                await self.apply_audio_settings({"source": src, "surround": sur})
                if not en:
                    await a.set_enabled(False)

    async def _play_sweep(self):
        rate = 48000
        t = np.arange(int(rate * 0.8)) / rate
        burst = (np.sin(2 * np.pi * 55 * t) * np.hanning(t.size)
                 * 32000).astype(np.int16)
        gap = np.zeros((int(rate * 0.25), 6), dtype=np.int16)
        order = [0, 1, 5, 4]  # FL -> FR -> RR -> RL, clockwise
        frames = []
        for ch in order:
            block = np.zeros((burst.size, 6), dtype=np.int16)
            block[:, ch] = burst
            frames.append(block)
            frames.append(gap)
        proc = await asyncio.create_subprocess_exec(
            "pacat", f"--device={SUR_SINK}", "--format=s16le",
            f"--rate={rate}", "--channels=6", f"--channel-map={SUR_MAP}",
            "--raw", stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            proc.stdin.write(np.concatenate(frames).tobytes())
            await proc.stdin.drain()
            proc.stdin.close()
        await proc.wait()

    def pad_rumble(self, amp, ms):
        """Gamepad FF mirrored to belly/mid rows — rumble, not a tap."""
        amp = min(1.0, max(0.0, float(amp)))
        ms = min(4000, max(60, int(ms)))
        if amp < 0.03:
            return
        front = [0.0] * 20
        for i in range(8, 20):  # rows 2-4: mid + belly
            front[i] = amp
        timeline = [("mapped", front, front[:])] * max(1, ms // TICK_MS)
        self.active["pad"] = Effect("pad", timeline)

    def vest_tap(self, args):
        """OSC /vest/tap <side> <amp 0-1> [ms] — a quick controller-rumble
        style tap on the upper chest, weighted to the given side."""
        side = str(args[0]).lower() if args else "both"
        amp = min(1.0, max(0.0, float(args[1]))) if len(args) > 1 else 0.5
        ms = min(2000, max(TICK_MS, int(args[2]))) if len(args) > 2 else 120
        if amp < 0.02:
            return
        wl = (1.0, 0.67, 0.33, 0.0)  # col 0 = wearer's left
        lv = amp if side in ("left", "both") else 0.0
        rv = amp if side in ("right", "both") else 0.0
        front = [0.0] * 20
        for i in range(8):  # upper two rows, like audio impacts
            w = wl[i % 4]
            front[i] = lv * w + rv * (1 - w)
        timeline = [("mapped", front, front[:])] * max(1, ms // TICK_MS)
        key = f"tap:{side}"
        self.active[key] = Effect(key, timeline)

    async def mixer(self):
        next_tick = time.monotonic()
        while True:
            front = [0.0] * 20
            back = [0.0] * 20
            raw = [0.0] * 40
            done = []
            for key, effect in self.active.items():
                cur = effect.current()
                if cur is None:
                    done.append(key)
                    continue
                if cur[0] == "mapped":
                    for i in range(20):
                        front[i] = max(front[i], cur[1][i])
                        back[i] = max(back[i], cur[2][i])
                else:
                    for i in range(40):
                        raw[i] = max(raw[i], cur[1][i])
            for key in done:
                self.active.pop(key, None)

            if self.osc_active:
                for i in range(20):
                    front[i] = max(front[i], self.osc_front[i])
                    back[i] = max(back[i], self.osc_back[i])
            elif self.osc_last and (any(self.osc_front) or any(self.osc_back)):
                self.osc_front = [0.0] * 20
                self.osc_back = [0.0] * 20

            a = self.audio
            if (a.enabled and not self.audio_suppressed
                    and (a.level_l or a.level_r or a.impact_l or a.impact_r
                         or a.level_bl or a.level_br
                         or a.impact_bl or a.impact_br)):
                l, r = a.level_l, a.level_r
                il, ir = a.impact_l, a.impact_r
                # surround: rear channels drive the back panel; if the game
                # only outputs stereo the rear is dead, so mirror the front
                use_rear = a.surround and a.rear_live
                bl, br = (a.level_bl, a.level_br) if use_rear else (l, r)
                ibl, ibr = (a.impact_bl, a.impact_br) if use_rear else (il, ir)
                bnd_bl = a.band_bl if use_rear else a.band_l
                bnd_br = a.band_br if use_rear else a.band_r
                wl = (1.0, 0.67, 0.33, 0.0)  # grid col 0 = wearer's left
                # spread mode: frequency becomes vertical position —
                # row 4 (belly) = sub, row 3 = bass, row 2 = lowmid
                row_band = {4: 0, 3: 1, 2: 2}
                for i in range(20):
                    w = wl[i % 4]
                    if a.spread:
                        bi = row_band.get(i // 4)
                        f_lvl = (a.band_l[bi] * w + a.band_r[bi] * (1 - w)
                                 if bi is not None else 0.0)
                        b_lvl = (bnd_bl[bi] * w + bnd_br[bi] * (1 - w)
                                 if bi is not None else 0.0)
                    else:
                        f_lvl = l * w + r * (1 - w)
                        b_lvl = bl * w + br * (1 - w)
                    front[i] = max(front[i], f_lvl)
                    back[i] = max(back[i], b_lvl)
                    if i < 8:  # impacts hit the upper two rows only
                        front[i] = max(front[i], il * w + ir * (1 - w))
                        back[i] = max(back[i], ibl * w + ibr * (1 - w))

            levels = raw[:]  # raw motor submits are already in physical order
            for i in range(20):
                fi, bi = self.mapping.front[i], self.mapping.back[i]
                levels[fi] = max(levels[fi], front[i])
                levels[bi] = max(levels[bi], back[i])
            motors = self.feel.shape(levels)

            self.last_motors = motors
            if any(motors):
                self.vest.mark_active()
            await self.vest.send(motors)
            # fixed-rate ticks: sleeping a flat 20 ms after the work + BLE
            # write made the real rate drift below 50 Hz
            next_tick += TICK_MS / 1000
            now = time.monotonic()
            if next_tick < now - 0.1:  # fell far behind (BLE stall): resync
                next_tick = now
            await asyncio.sleep(max(0.0, next_tick - now))

    def persisted_settings(self):
        """What goes to audio_settings.json: while a game is auto-bound, the
        pre-game setup (plus current global toggles) — so a shutdown mid-game
        doesn't make that game's source and tuning the new defaults."""
        cur = self.audio.settings()
        sess = self.auto_session
        if not sess:
            return cur
        base = dict(sess["saved"])
        base.update({k: cur[k] for k in GLOBAL_KEYS if k in cur})
        return base

    async def _auto_bind(self, key, name, preset=None):
        """Bind audio capture to a playing app (optionally via its preset)."""
        a = self.audio
        prev = self.auto_session
        if prev:  # switching games: keep what was tuned for the old one
            self._auto_remember(prev)
        cfg = strip_global(self.presets[preset] if preset else a.learned.get(key) or {})
        cfg["source"] = "appname:" + name
        saved = prev["saved"] if prev else a.settings()
        saved_preset = prev["saved_preset"] if prev else self.active_preset
        await self.apply_audio_settings(cfg)
        await a.set_enabled(True)
        self.active_preset = preset or ""
        self.auto_session = {"app": key, "preset": preset, "saved": saved,
                             "saved_preset": saved_preset, "start": a._snapshot()}
        self.auto_profile_app = name

    def _auto_remember(self, sess):
        # only store tuning the user actually changed; an untouched game
        # must keep following the global settings
        if not sess["preset"] and self.audio._snapshot() != sess["start"]:
            self.audio.remember_app(sess["app"])

    async def _auto_end(self, restore=True):
        sess, self.auto_session = self.auto_session, None
        self.auto_profile_app = ""
        if sess is None:
            return
        self._auto_remember(sess)
        if restore:
            a = self.audio
            await self.apply_audio_settings(strip_global(sess["saved"]))
            await a.set_enabled(a.start_enabled)
            self.active_preset = sess["saved_preset"]

    async def autoprofile_loop(self):
        """Auto-apply a preset when an app whose name matches one starts playing.

        Name a preset after the app shown in `vestctl sources` (case-insensitive)
        and it gets applied — source bound to that app — the moment the game
        makes sound; the previous audio setup is restored when it exits.

        Fallback when no preset matches: the most game-like playing app (Steam
        game > Proton exe > anything not in NON_GAME_APPS) gets the capture
        bound with its remembered tuning. Same restore-on-exit rules; a user
        source/preset change hands the app over to them until it stops.
        """
        loop = asyncio.get_running_loop()
        suppressed = set()  # apps the user overrode; skip until their stream is gone
        while True:
            await asyncio.sleep(AUTOPROFILE_POLL)
            a = self.audio
            if not a.auto_profiles:
                if self.auto_session:
                    await self._auto_end(restore=False)
                continue
            rows = await loop.run_in_executor(None, list_audio_apps)
            apps = {r["key"]: r["name"] for r in rows}
            order = [r["key"] for r in rows]  # newest stream first
            steam_names = await loop.run_in_executor(None, installed_game_names)
            strength = {r["key"]: game_strength(r, steam_names) for r in rows}
            suppressed &= set(apps)
            sess = self.auto_session
            if sess:
                if sess["app"] not in apps:
                    await self._auto_end()
                    log.info("auto profile: %r closed, previous audio setup restored",
                             sess["preset"] or sess["app"])
                    self.notify("Haptics off", f"{sess['app']} closed",
                                urgency="low", replace="bhaptics-game")
                elif not a.start_enabled:
                    # user switched audio haptics off mid-game: stay off, but
                    # put the pre-game source back so a restart isn't pinned
                    await self._auto_end()
                    log.info("auto profile: audio turned off, released %s", sess["app"])
                elif (self.active_preset != (sess["preset"] or "")
                      or a.source != "appname:" + apps[sess["app"]]):
                    # user picked another preset/source while active: hands off
                    await self._auto_end(restore=False)
                    suppressed.add(sess["app"])
                elif not sess["preset"]:
                    # a more game-like stream appeared (e.g. a Steam game after
                    # a generic process): move to it rather than staying stuck
                    cur = strength.get(sess["app"], 0)
                    better = next((k for k in order
                                   if k not in suppressed
                                   and strength.get(k, 0) > cur), None)
                    if better is not None:
                        await self._auto_bind(better, apps[better])
                        log.info("auto follow: switching to %s", apps[better])
                        self.notify(f"Haptics: {apps[better]}",
                                    "audio haptics active — tune it and it'll be kept",
                                    replace="bhaptics-game")
                continue
            if not a.start_enabled:
                continue  # audio haptics switched off: off means off
            preset_by_app = {n.lower(): n for n in self.presets}
            match = next((k for k in order
                          if k not in suppressed and k in preset_by_app), None)
            if match is not None:
                name = preset_by_app[match]
                await self._auto_bind(match, apps[match], preset=name)
                log.info("auto profile: %r applied for %s", name, apps[match])
                self.notify(f"Haptics: {name}", f"preset active for {apps[match]}",
                            replace="bhaptics-game")
                continue
            if self.audio_suppressed:
                continue
            bound = (a.source[len("appname:"):].lower()
                     if a.source.startswith("appname:") else None)
            if bound and bound in apps:
                continue  # manually bound app still playing — leave it
            best = None
            for r in rows:
                if r["key"] in suppressed:
                    continue
                st = strength.get(r["key"], 0)
                if best is None or st > best[0]:
                    best = (st, r)  # newest wins ties (rows are newest-first)
            if best is not None:
                key, name = best[1]["key"], best[1]["name"]
                learned = key in a.learned
                await self._auto_bind(key, name)
                log.info("auto follow: bound to %s (strength %d%s)", name, best[0],
                         ", remembered" if learned else "")
                self.notify(f"Haptics: {name}",
                            "audio haptics active — tune it and it'll be kept",
                            replace="bhaptics-game")

    async def meter_loop(self):
        while True:
            if self.meter_subs:
                a = self.audio
                msg = json.dumps({"AudioMeter": {
                    "on": a.enabled, "rel": round(a.rel, 3), "thresh": round(a.thresh, 3),
                    "outL": round(a.level_l, 3), "outR": round(a.level_r, 3),
                    "impL": round(a.impact_l, 3), "impR": round(a.impact_r, 3),
                    "outBL": round(a.level_bl, 3), "outBR": round(a.level_br, 3),
                    "duty": round(a.duty, 3), "agc": a.agc,
                    "surround": a.surround, "rearLive": a.rear_live,
                    "suppressed": self.audio_suppressed,
                    "waiting": a.enabled and a.waiting,
                    "vestGated": a.vest_gated,
                }, "Motors": self.last_motors})
                for ws in list(self.meter_subs):
                    with contextlib.suppress(Exception):
                        await ws.send(msg)
            await asyncio.sleep(0.05)

    async def status_loop(self):
        last = None
        saved_audio = self.persisted_settings()
        last_conn = None
        last_sup = None
        sup_since = None
        paused_notice = float("-inf")
        while True:
            msg = self.status_message()
            if msg != last and self.clients:
                for ws in list(self.clients):
                    with contextlib.suppress(Exception):
                        await ws.send(msg)
                last = msg
            cur = self.persisted_settings()
            if cur != saved_audio:
                with contextlib.suppress(OSError):
                    atomic_write_json(AUDIO_CONF, cur)
                    saved_audio = cur
            # notify on meaningful transitions only (not every reconnect loop)
            conn = self.vest.connected
            if last_conn is not None and conn != last_conn:
                if conn:
                    bat = (f" · battery {self.vest.battery}%"
                           if self.vest.battery is not None else "")
                    self.notify("Vest connected", f"haptics ready{bat}",
                                replace="bhaptics-vest")
                elif not self.vest.idle:
                    self.notify("Vest disconnected", "searching for the vest…",
                                icon="bluetooth", replace="bhaptics-vest")
            last_conn = conn
            sup = self.audio_suppressed
            now = time.monotonic()
            # every game pattern opens an 8 s window, so a game hitting you
            # every ~20 s flaps this: tell once per stretch of play, and only
            # announce "resumed" after a long game-driven stretch
            if sup and not last_sup:
                sup_since = now
                if self.audio.enabled and now - paused_notice > 600:
                    self.notify("Game controls the vest",
                                "audio haptics paused while the game sends patterns",
                                urgency="low", replace="bhaptics-sup")
                    paused_notice = now
            elif not sup and last_sup and sup_since is not None:
                if self.audio.enabled and now - sup_since > 60:
                    self.notify("Audio haptics resumed", "vest is back on game audio",
                                urgency="low", replace="bhaptics-sup")
            last_sup = sup
            await asyncio.sleep(1)


async def ws_handler(ws, state):
    from urllib.parse import parse_qs

    q = parse_qs(urlparse(ws.request.path).query)
    app_id = (q.get("app_id") or [""])[0]
    app_name = (q.get("app_name") or [""])[0]
    is_game = app_id != "ui"
    # UI/tray/vestctl reconnect constantly (the tray polls every 2 s): debug only
    (log.info if is_game else log.debug)("client connected: %s%s", ws.request.path,
                                         " (game)" if is_game else "")
    if is_game and state.relay_host:
        await state.wait_for_remote()
        if state.host_remote:
            try:
                await state.proxy_sdk1(ws)
            except Exception as e:
                state.relay_error = str(e)
                log.warning("relay host: SDK1 proxy failed: %s", e)
            return
    state.clients.add(ws)
    if is_game:
        state.game_clients[ws] = app_name or app_id or "unnamed client"
        state.vest.mark_active()  # a game is here: wake the vest if it slept
        log.info("game client connected: %s (audio yields while it sends patterns)",
                 app_name or app_id or "unnamed client")
    try:
        await ws.send(state.status_message())
        async for raw in ws:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("bad json: %.120s", raw)
                continue
            if payload.get("Subscribe") == "audiometer":
                state.meter_subs.add(ws)
                continue
            if payload.get("ListAudioSources"):
                sources = await asyncio.get_running_loop().run_in_executor(
                    None, state.list_audio_sources)
                await ws.send(json.dumps({"AudioSources": sources}))
                continue
            if payload.get("GetEffect"):
                name = str(payload["GetEffect"])
                await ws.send(json.dumps(
                    {"Effect": {"name": name, "frames": state.effects.get(name, [])}}))
                continue
            if payload.get("DoctorScan") or payload.get("DoctorFix"):
                target = str(payload.get("DoctorFix") or "")
                if target and not re.fullmatch(r"\d+", target):
                    continue
                vc = BASE_DIR / "vestctl.py"
                cmd = ([sys.executable, str(vc)] if vc.exists()
                       else [shutil.which("vestctl") or "vestctl"])
                cmd += ["doctor", "--json"] + ([target, "--fix"] if target else ["--all"])

                def run_doctor(c=cmd):
                    try:
                        return subprocess.run(c, capture_output=True, text=True, timeout=300)
                    except Exception as e:
                        return e

                res = await asyncio.get_running_loop().run_in_executor(None, run_doctor)
                report = None
                if isinstance(res, subprocess.CompletedProcess) and res.returncode == 0:
                    with contextlib.suppress(ValueError):
                        report = json.loads(res.stdout)
                if report is not None:
                    await ws.send(json.dumps({"DoctorReport": report,
                                              "Fixed": target or None}))
                else:
                    err = (res.stderr.strip()[-200:]
                           if isinstance(res, subprocess.CompletedProcess) else str(res))
                    await ws.send(json.dumps({"ImportNote": f"🩺 doctor failed: {err}"}))
                continue
            try:
                await state.handle(payload, game=is_game)
            except Exception:
                # one malformed field (e.g. intensityRatio: null) must not
                # drop the game's whole connection
                log.exception("bad message from %s: %.200s", app_name or app_id, raw)
                continue
            if "ImportTact" in payload:
                await ws.send(json.dumps({"ImportNote": state.import_note}))
            await ws.send(state.status_message())
    except websockets.ConnectionClosed:
        pass
    finally:
        state.clients.discard(ws)
        state.meter_subs.discard(ws)
        state.game_clients.pop(ws, None)
        (log.info if is_game else log.debug)("client disconnected")


async def sdk2_handler(ws, state):
    from urllib.parse import parse_qs

    q = parse_qs(urlparse(ws.request.path).query)
    workspace_id = (q.get("workspace_id") or [""])[0]
    api_key = (q.get("api_key") or [""])[0]
    if state.relay_host:
        await state.wait_for_remote()
        if state.host_remote:
            try:
                await state.proxy_sdk2(ws)
            except Exception as e:
                state.relay_error = str(e)
                log.warning("relay host: SDK2 proxy failed: %s", e)
            return
    log.info("sdk2 client connected: workspace_id=%r", workspace_id)
    state.game_clients[ws] = f"SDK2: {workspace_id or 'unknown'}"
    state.vest.mark_active()  # a game is here: wake the vest if it slept
    try:
        async for raw in ws:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("sdk2 bad json: %.120s", raw)
                continue
            try:
                await state.handle_sdk2(ws, payload, workspace_id, api_key)
            except websockets.ConnectionClosed:
                raise
            except Exception:
                log.exception("sdk2 bad message: %.200s", raw)
    except websockets.ConnectionClosed:
        pass
    finally:
        state.game_clients.pop(ws, None)
        log.info("sdk2 client disconnected")


def sdk2_ssl_context():
    """SDK2 clients require wss:// but skip certificate validation."""
    import ssl

    cert = STATE_DIR / "sdk2_cert.pem"
    key = STATE_DIR / "sdk2_key.pem"
    if not (cert.exists() and key.exists()):
        log.info("generating self-signed TLS cert for SDK2 port")
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(key), "-out", str(cert), "-days", "3650",
             "-subj", "/CN=127.0.0.1"],
            check=True, capture_output=True,
        )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    return ctx


def _is_loopback(addr):
    if not addr:
        return False
    host = addr[0]
    return host == "::1" or host.startswith("127.")


def process_request(connection, request):
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return None
    path = urlparse(request.path).path
    if path in ("/", "/ui", "/ui.html"):
        # The control UI shares the SDK1 port; never serve it off-box even when
        # the SDK endpoints are LAN-bound for Remote Play.
        if not _is_loopback(connection.remote_address):
            return connection.respond(http.HTTPStatus.FORBIDDEN, "UI is local-only\n")
        try:
            body = UI_FILE.read_text()
        except OSError:
            return connection.respond(http.HTTPStatus.NOT_FOUND, "ui.html missing\n")
        resp = connection.respond(http.HTTPStatus.OK, body)
        with contextlib.suppress(KeyError):
            del resp.headers["Content-Type"]
        resp.headers["Content-Type"] = "text/html; charset=utf-8"
        return resp
    return connection.respond(http.HTTPStatus.NOT_FOUND, "not found\n")


async def supervise(name, fn):
    """Run a forever-loop; if it crashes, log it and restart it rather than
    leaving the daemon half-dead (e.g. auto-follow silently gone)."""
    while True:
        try:
            await fn()
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s loop crashed — restarting in 2 s", name)
            await asyncio.sleep(2)


class DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        parts = data.decode(errors="replace").split()
        if len(parts) != 3 or parts[0] != "BHAPTICS-LINUX/1" or parts[1] != "DISCOVER":
            return
        reply = (f"BHAPTICS-LINUX/1 OFFER {parts[2]} {socket.gethostname()} "
                 f"{SDK1_PORT} {SDK2_PORT} {OSC_PORT}")
        self.transport.sendto(reply.encode(), addr)


def remote_host_from_config():
    try:
        return str(json.loads(NETWORK_FILE.read_text()).get("remote") or "")
    except (OSError, ValueError, TypeError):
        return ""


async def discover_vest_host(timeout=1.5):
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", 0))
    nonce = os.urandom(8).hex()
    found = []

    class Probe(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            parts = data.decode(errors="replace").split()
            if (len(parts) == 7 and parts[0] == "BHAPTICS-LINUX/1" and parts[1] == "OFFER"
                    and parts[2] == nonce and parts[3] != socket.gethostname()
                    and addr[0] not in ("127.0.0.1", "::1")):
                found.append(addr[0])

    transport, _ = await loop.create_datagram_endpoint(Probe, sock=sock)
    with contextlib.suppress(OSError):
        transport.sendto(f"BHAPTICS-LINUX/1 DISCOVER {nonce}".encode(),
                         ("255.255.255.255", DISCOVERY_PORT))
    await asyncio.sleep(timeout)
    transport.close()
    return found[0] if found else None


def sdk2_client_ssl_context():
    import ssl

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # websockets logs every open/close/HTTP request at INFO: journal noise
    logging.getLogger("websockets").setLevel(logging.WARNING)
    migrate_legacy_state()
    vest = VestLink()
    state = PlayerState(vest)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, stop.set)

    if state.audio.start_enabled:
        await state.audio.set_enabled(True)
    if state.audio.pad_mirror:
        await state.pad.set_enabled(True)

    listen_host = bind_address()
    if listen_host not in ("127.0.0.1", "::1", "localhost"):
        log.warning("listening on %s: SDK1/SDK2/OSC are unauthenticated — "
                    "trusted networks only (Remote Play), see README", listen_host)
        try:
            await loop.create_datagram_endpoint(
                DiscoveryProtocol, local_addr=("0.0.0.0", DISCOVERY_PORT))
            log.info("discovery: answering LAN probes on udp://0.0.0.0:%d", DISCOVERY_PORT)
        except OSError as e:
            log.warning("discovery port %d unavailable (%s)", DISCOVERY_PORT, e)

    try:
        await loop.create_datagram_endpoint(
            lambda: OscProtocol(state), local_addr=(listen_host, OSC_PORT))
        log.info("OSC (VRChat) listening on udp://%s:%d", listen_host, OSC_PORT)
    except OSError as e:
        log.warning("OSC port %d unavailable (%s) — VRChat bridge disabled", OSC_PORT, e)

    tasks = [asyncio.create_task(supervise(name, fn)) for name, fn in (
        ("vest link", vest.maintain),
        ("mixer", state.mixer),
        ("status", state.status_loop),
        ("meter", state.meter_loop),
        ("auto profile", state.autoprofile_loop),
    )]

    # big max_size: definition manifests and Register payloads can be MBs.
    # ping_interval=None: game SDK clients don't answer RFC6455 pings (the
    # official Player never sends any), so the default 20s ping + 20s timeout
    # killed every game connection after exactly 40s.
    async with websockets.serve(
        lambda ws: ws_handler(ws, state), listen_host, SDK1_PORT,
        process_request=process_request, max_size=16 * 2**20,
        ping_interval=None,
    ), websockets.serve(
        lambda ws: sdk2_handler(ws, state), listen_host, SDK2_PORT,
        ssl=sdk2_ssl_context(), max_size=16 * 2**20,
        ping_interval=None,
    ):
        log.info("SDK1 on ws://%s:%d/v2/feedbacks — local UI at http://127.0.0.1:%d/ui",
                 listen_host, SDK1_PORT, SDK1_PORT)
        log.info("SDK2 on wss://%s:%d/v3/feedback", listen_host, SDK2_PORT)
        await stop.wait()

    await state.audio.set_enabled(False)
    state.stop_relay_host()
    for task in tasks:
        task.cancel()
    if vest.connected:
        with contextlib.suppress(Exception):
            await vest.client.write_gatt_char(MOTOR_STABLE, bytes(20), response=False)
            await vest.client.disconnect()
    log.info("bye")


if __name__ == "__main__":
    asyncio.run(main())
