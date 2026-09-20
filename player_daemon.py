"""bHaptics Player emulator for Linux: SDK1 WebSocket API -> direct BLE.

Listens on ws://127.0.0.1:15881/v2/feedbacks (the endpoint bHaptics-enabled
games and mods connect to) and drives a TactSuit over BLE, no Windows Player
needed. Also serves a test/mapping UI at http://127.0.0.1:15881/ui.

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
import contextlib
import http
import json
import logging
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
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

__version__ = "0.0.1"

BASE_DIR = Path(__file__).resolve().parent
UI_FILE = BASE_DIR / "ui.html"

# user files live in XDG dirs so the daemon can run from a read-only install
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME",
                                 Path.home() / ".config")) / "bhaptics-linux"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME",
                                Path.home() / ".local/state")) / "bhaptics-linux"
MAPPING_FILE = CONFIG_DIR / "mapping.json"
PATTERNS_DIR = CONFIG_DIR / "patterns"


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
            MAPPING_FILE.write_text(json.dumps({"front": front, "back": back}))
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
            i = grid_to_dot(float(p.get("X", 0)), float(p.get("Y", 0)))
            target[i] = max(target[i], norm_intensity(p.get("Intensity", 0)))

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
            else:  # path: step through the point list by time
                pts = payload
                rel = t - s
                chosen = pts[0]
                for p in pts:
                    if int(g(p, "Time", 0)) <= rel:
                        chosen = p
                i = grid_to_dot(float(g(chosen, "X", 0)), float(g(chosen, "Y", 0)))
                target[i] = max(
                    target[i], norm_intensity(g(chosen, "Intensity", 0)) * intensity_ratio
                )
        timeline.append(("mapped", front, back))
    return timeline


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

    def datagram_received(self, data, addr):
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

    @property
    def connected(self):
        return self.client is not None and self.client.is_connected

    async def maintain(self):
        while True:
            if not self.connected:
                try:
                    device = await BleakScanner.find_device_by_filter(
                        lambda d, adv: (d.name or "").startswith(VEST_NAME_PREFIXES),
                        timeout=10.0,
                    )
                    if device is None:
                        await asyncio.sleep(3)
                        continue
                    client = BleakClient(device, timeout=20.0)
                    await client.connect()
                    self.client = client
                    self.last_sent = None
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
                self._batt_tick = getattr(self, "_batt_tick", 0) + 1
                if self._batt_tick >= 60:
                    self._batt_tick = 0
                    await self.read_battery()

    async def read_battery(self):
        if not self.connected:
            return
        with contextlib.suppress(Exception):
            data = await self.client.read_gatt_char(BATTERY_CHAR)
            if data:
                self.battery = data[0]

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
EFFECTS_FILE = CONFIG_DIR / "effects.json"
OSC_PORT = 9001  # VRChat sends avatar parameters here
OSC_RE = re.compile(r"^/avatar/parameters/bOSC/v2/(VestFront|VestBack)/(\d+)$")


SUR_SINK = "vest51"
SUR_MAP = "front-left,front-right,front-center,lfe,rear-left,rear-right"


def pactl(*args):
    return subprocess.run(["pactl", *args],
                          capture_output=True, text=True).stdout


def sink_inputs():
    """Parsed `pactl list sink-inputs`; [] when pactl fails."""
    try:
        return json.loads(subprocess.run(
            ["pactl", "-f", "json", "list", "sink-inputs"],
            capture_output=True, text=True).stdout)
    except Exception:
        return []


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
        self.source = "default"
        self.start_enabled = True
        self.auto_profiles = True
        self.pad_mirror = False
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
        # legacy raw stream indexes don't survive reboots (appname: does)
        if self.source.startswith("app:"):
            self.source = "default"
        self.waiting = False

    def settings(self):
        return {"gain": self.gain, "floor": self.floor,
                "max": self.max_level, "stereo": self.stereo,
                "enabled": self.enabled, "source": self.source,
                "impact": self.impact, "impGain": self.impact_gain,
                "impMax": self.impact_max, "auto": self.auto_profiles,
                "spread": self.spread, "pad": self.pad_mirror,
                "agc": self.agc, "activity": self.activity,
                "surround": self.surround}

    async def set_enabled(self, on):
        if on and not self.enabled:
            self.enabled = True
            self._task = asyncio.create_task(self._run())
            log.info("audio mode ON (source %s)", self.source)
        elif not on and self.enabled:
            self.enabled = False
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
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
        if self._proc is not None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.terminate()
            self._proc = None

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
            target = self._resolve_target()
            if target is None:
                if not announced:
                    log.info("audio: waiting for %s to play audio...", self.source)
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
    """Apps currently playing audio: lowercased name -> original name."""
    out = {}
    for si in sink_inputs():
        props = si.get("properties", {})
        app = (props.get("application.name")
               or props.get("application.process.binary") or "")
        if app:
            out.setdefault(app.lower(), app)
    return out


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
        cooldown = {}  # dev path -> monotonic time its last mirror task ended
        # probe each node once; inode changes when a device is re-created at
        # the same path, so genuinely new hardware still gets looked at
        rejected = set()  # (path, inode) of nodes without rumble
        while True:
            for path, t in list(self.tasks.items()):
                if t.done():
                    self.tasks.pop(path)
                    self.names.pop(path, None)
                    cooldown[path] = time.monotonic()
            for path in evdev.list_devices():
                if path in self.tasks:
                    continue
                try:
                    ino = os.stat(path).st_ino
                except OSError:
                    continue
                if (path, ino) in rejected:
                    continue
                last = cooldown.get(path)
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
            log.warning("pad mirror %s failed: %s (need /dev/uinput access?)", path, e)
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


class PlayerState:
    def __init__(self, vest):
        self.vest = vest
        self.mapping = VestMapping()
        self.audio = AudioEngine()
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
        self.sdk2_events = {}  # eventName -> project dict
        self.auto_profile_app = ""  # display name while an auto profile is active
        self.pad = PadMirror(self)
        self.patterns = set()  # keys loaded from patterns/*.tact (persisted)
        self.missing_keys = {}  # names games asked for but we don't have
        self.import_note = ""
        self._load_sdk2_cache()
        self._load_patterns()  # after cache: explicit imports win over cache

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
        for f in sorted(SDK2_CACHE.glob("*.json")):
            with contextlib.suppress(Exception):
                self.sdk2_events.update(extract_events(json.loads(f.read_text())))
        if self.sdk2_events:
            log.info("sdk2: %d events restored from cache", len(self.sdk2_events))

    @property
    def osc_active(self):
        return (time.monotonic() - self.osc_last) < 5.0

    @property
    def audio_suppressed(self):
        return bool(self.game_clients) or self.osc_active

    def list_audio_sources(self):
        out = [{"value": "default", "label": "System output (default)"}]
        try:
            sinks = json.loads(subprocess.run(
                ["pactl", "-f", "json", "list", "sinks"],
                capture_output=True, text=True).stdout)
            for s in sinks:
                out.append({"value": f"sink:{s['name']}",
                            "label": f"Output: {s.get('description', s['name'])}"})
            inputs = json.loads(subprocess.run(
                ["pactl", "-f", "json", "list", "sink-inputs"],
                capture_output=True, text=True).stdout)
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
                "Mapping": self.mapping.as_dict(),
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

    async def handle(self, payload):
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
            elif stype == "raw":
                timeline = compile_raw_submit(sub.get("Frame") or {})
                if timeline:
                    self.active[key or "raw"] = Effect(key, timeline)
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

        if "SetMapping" in payload:
            try:
                m = payload["SetMapping"]
                self.mapping.set(m["front"], m["back"])
            except Exception as e:
                log.warning("bad SetMapping: %s", e)

        if "AudioMode" in payload:
            am = payload["AudioMode"]
            if isinstance(am, dict):
                await self.apply_audio_settings(am)
                if any(k not in ("auto", "enabled") for k in am):
                    self.active_preset = ""
                enabled = bool(am.get("enabled", self.audio.enabled))
            else:
                enabled = bool(am)
            await self.audio.set_enabled(enabled)

        if "PadMirror" in payload:
            self.audio.pad_mirror = bool(payload["PadMirror"])
            await self.pad.set_enabled(self.audio.pad_mirror)

        if "SavePreset" in payload:
            name = str(payload["SavePreset"]).strip()
            if name:
                snap = self.audio.settings()
                snap.pop("enabled", None)
                snap.pop("auto", None)  # auto-profile is global, not per-preset
                snap.pop("pad", None)   # pad mirror is global too
                self.presets[name] = snap
                self._save_presets()
                self.active_preset = name
                log.info("preset saved: %r", name)

        if "ApplyPreset" in payload:
            name = str(payload["ApplyPreset"])
            preset = self.presets.get(name)
            if preset:
                await self.apply_audio_settings(preset)
                self.active_preset = name
                log.info("preset applied: %r", name)

        if "DeletePreset" in payload:
            name = str(payload["DeletePreset"])
            if self.presets.pop(name, None) is not None:
                self._save_presets()
                if self.active_preset == name:
                    self.active_preset = ""
                log.info("preset deleted: %r", name)

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
            timeline = compile_project(project) if project is not None else []
            if timeline:
                self.active[f"sdk2ui:{name}"] = Effect(name, timeline)

    def _save_effects(self):
        with contextlib.suppress(OSError):
            EFFECTS_FILE.write_text(json.dumps(
                {k: v for k, v in self.effects.items() if not k.startswith("__")}))

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
        a.activity = min(10.0, max(1.0, float(d.get("activity", a.activity))))
        new_src = str(d.get("source", a.source))
        new_sur = bool(d.get("surround", a.surround))
        if new_src != a.source or new_sur != a.surround:
            was_enabled = a.enabled
            await a.set_enabled(False)
            if a.surround and not new_sur:
                a.teardown_surround()
            a.source, a.surround = new_src, new_sur
            if was_enabled:
                await a.set_enabled(True)

    def _save_presets(self):
        with contextlib.suppress(OSError):
            PRESETS_FILE.write_text(json.dumps(self.presets, indent=1))

    def ingest_sdk2_defs(self, data, source):
        if data is None:
            return
        SDK2_CACHE.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (SDK2_CACHE / f"{source}-{stamp}.json").write_text(
            json.dumps(data) if not isinstance(data, str) else data
        )
        events = extract_events(data)
        if events:
            self.sdk2_events.update(events)
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
                self.ingest_sdk2_defs(g(inner, "Haptic"), f"auth-{workspace_id}")
            if workspace_id and not self.sdk2_events:
                data = await asyncio.get_running_loop().run_in_executor(
                    None, fetch_sdk2_definitions, workspace_id, api_key
                )
                if data:
                    self.ingest_sdk2_defs(g(data, "Message", data), f"cloud-{workspace_id}")
            await reply("ServerReady", "")
            await send_devices()
            event_list = [
                {"eventName": name, "eventTime": len(compile_project(p)) * TICK_MS if p else 1000}
                for name, p in self.sdk2_events.items()
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

        elif mtype == "SdkPlay":
            name = g(inner, "EventName", "")
            req_id = g(inner, "RequestId", 0)
            intensity = float(g(inner, "Intensity", 1.0))
            duration = float(g(inner, "Duration", 1.0)) or 1.0
            project = self.sdk2_events.get(name)
            timeline = []
            if project is not None:
                timeline = compile_project(
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

            motors = [0] * 40
            for i in range(20):
                motors[self.mapping.front[i]] = max(
                    motors[self.mapping.front[i]], round(front[i] * 15)
                )
                motors[self.mapping.back[i]] = max(
                    motors[self.mapping.back[i]], round(back[i] * 15)
                )
            for i in range(40):
                motors[i] = max(motors[i], round(raw[i] * 15))

            self.last_motors = motors
            await self.vest.send(motors)
            await asyncio.sleep(TICK_MS / 1000)

    async def autoprofile_loop(self):
        """Auto-apply a preset when an app whose name matches one starts playing.

        Name a preset after the app shown in `vestctl sources` (case-insensitive)
        and it gets applied — source bound to that app — the moment the game
        makes sound; the previous audio setup is restored when it exits.
        """
        loop = asyncio.get_running_loop()
        current = None  # {"app", "preset", "saved", "saved_enabled", "saved_preset"}
        suppressed = set()  # apps the user overrode; skip until their stream is gone
        while True:
            await asyncio.sleep(4)
            a = self.audio
            if not a.auto_profiles:
                if current:
                    current = None
                    self.auto_profile_app = ""
                continue
            apps = await loop.run_in_executor(None, list_audio_apps)
            suppressed &= set(apps)
            if current:
                expect_src = "appname:" + apps.get(current["app"], "")
                if current["app"] not in apps:
                    await self.apply_audio_settings(current["saved"])
                    await a.set_enabled(current["saved_enabled"])
                    self.active_preset = current["saved_preset"]
                    log.info("auto profile: %r closed, previous audio setup restored",
                             current["preset"])
                    current = None
                    self.auto_profile_app = ""
                elif (self.active_preset != current["preset"]
                      or a.source != expect_src or not a.enabled):
                    # user changed preset/source/off while active: hands off
                    suppressed.add(current["app"])
                    current = None
                    self.auto_profile_app = ""
                continue
            for name in sorted(self.presets):
                key = name.lower()
                if key not in apps or key in suppressed:
                    continue
                saved = a.settings()
                saved_enabled = a.enabled
                saved_preset = self.active_preset
                cfg = dict(self.presets[name])
                cfg["source"] = "appname:" + apps[key]
                await self.apply_audio_settings(cfg)
                await a.set_enabled(True)
                self.active_preset = name
                current = {"app": key, "preset": name, "saved": saved,
                           "saved_enabled": saved_enabled, "saved_preset": saved_preset}
                self.auto_profile_app = apps[key]
                log.info("auto profile: %r applied for %s", name, apps[key])
                break

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
                }, "Motors": self.last_motors})
                for ws in list(self.meter_subs):
                    with contextlib.suppress(Exception):
                        await ws.send(msg)
            await asyncio.sleep(0.05)

    async def status_loop(self):
        last = None
        saved_audio = self.audio.settings()
        while True:
            msg = self.status_message()
            if msg != last and self.clients:
                for ws in list(self.clients):
                    with contextlib.suppress(Exception):
                        await ws.send(msg)
                last = msg
            cur = self.audio.settings()
            if cur != saved_audio:
                with contextlib.suppress(OSError):
                    AUDIO_CONF.write_text(json.dumps(cur))
                    saved_audio = cur
            await asyncio.sleep(1)


async def ws_handler(ws, state):
    from urllib.parse import parse_qs

    q = parse_qs(urlparse(ws.request.path).query)
    app_id = (q.get("app_id") or [""])[0]
    app_name = (q.get("app_name") or [""])[0]
    is_game = app_id != "ui"
    log.info("client connected: %s%s", ws.request.path, " (game)" if is_game else "")
    state.clients.add(ws)
    if is_game:
        state.game_clients[ws] = app_name or app_id or "unnamed client"
        if state.audio.enabled:
            log.info("audio mode suppressed: game client connected")
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
                await ws.send(json.dumps({"AudioSources": state.list_audio_sources()}))
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
            await state.handle(payload)
            if "ImportTact" in payload:
                await ws.send(json.dumps({"ImportNote": state.import_note}))
            await ws.send(state.status_message())
    except websockets.ConnectionClosed:
        pass
    finally:
        state.clients.discard(ws)
        state.meter_subs.discard(ws)
        state.game_clients.pop(ws, None)
        if is_game and not state.audio_suppressed and state.audio.enabled:
            log.info("audio mode resumed: no game clients left")
        log.info("client disconnected")


async def sdk2_handler(ws, state):
    from urllib.parse import parse_qs

    q = parse_qs(urlparse(ws.request.path).query)
    workspace_id = (q.get("workspace_id") or [""])[0]
    api_key = (q.get("api_key") or [""])[0]
    log.info("sdk2 client connected: workspace_id=%r", workspace_id)
    state.game_clients[ws] = f"SDK2: {workspace_id or 'unknown'}"
    try:
        async for raw in ws:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("sdk2 bad json: %.120s", raw)
                continue
            await state.handle_sdk2(ws, payload, workspace_id, api_key)
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


def process_request(connection, request):
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return None
    path = urlparse(request.path).path
    if path in ("/", "/ui", "/ui.html"):
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


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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

    try:
        await loop.create_datagram_endpoint(
            lambda: OscProtocol(state), local_addr=("127.0.0.1", OSC_PORT))
        log.info("OSC (VRChat) listening on udp://127.0.0.1:%d", OSC_PORT)
    except OSError as e:
        log.warning("OSC port %d unavailable (%s) — VRChat bridge disabled", OSC_PORT, e)

    tasks = [
        asyncio.create_task(vest.maintain()),
        asyncio.create_task(state.mixer()),
        asyncio.create_task(state.status_loop()),
        asyncio.create_task(state.meter_loop()),
        asyncio.create_task(state.autoprofile_loop()),
    ]

    # big max_size: definition manifests and Register payloads can be MBs
    async with websockets.serve(
        lambda ws: ws_handler(ws, state), "127.0.0.1", 15881,
        process_request=process_request, max_size=16 * 2**20,
    ), websockets.serve(
        lambda ws: sdk2_handler(ws, state), "127.0.0.1", 15882,
        ssl=sdk2_ssl_context(), max_size=16 * 2**20,
    ):
        log.info("SDK1 on ws://127.0.0.1:15881/v2/feedbacks — UI at http://127.0.0.1:15881/ui")
        log.info("SDK2 on wss://127.0.0.1:15882/v3/feedback")
        await stop.wait()

    await state.audio.set_enabled(False)
    for task in tasks:
        task.cancel()
    if vest.connected:
        with contextlib.suppress(Exception):
            await vest.client.write_gatt_char(MOTOR_STABLE, bytes(20), response=False)
            await vest.client.disconnect()
    log.info("bye")


if __name__ == "__main__":
    asyncio.run(main())
