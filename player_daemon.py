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
import re
import signal
import struct
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import websockets
from bleak import BleakClient, BleakScanner

log = logging.getLogger("bhaptics-daemon")

BASE_DIR = Path(__file__).resolve().parent
MAPPING_FILE = BASE_DIR / "mapping.json"
UI_FILE = BASE_DIR / "ui.html"

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
            self.client = None


AUDIO_RATE = 48000
AUDIO_CHUNK = 960  # 20 ms
AUDIO_CONF = BASE_DIR / "audio_settings.json"
PRESETS_FILE = BASE_DIR / "audio_presets.json"
EFFECTS_FILE = BASE_DIR / "effects.json"
OSC_PORT = 9001  # VRChat sends avatar parameters here
OSC_RE = re.compile(r"^/avatar/parameters/bOSC/v2/(VestFront|VestBack)/(\d+)$")


class AudioEngine:
    """Captures the default sink monitor, exposes bass level as 0..1."""

    def __init__(self):
        self.enabled = False
        self.stereo = True
        self.level_l = 0.0
        self.level_r = 0.0
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
        self._task = None
        self._proc = None
        self.source = "default"
        self.start_enabled = True
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
        # legacy raw stream indexes don't survive reboots (appname: does)
        if self.source.startswith("app:"):
            self.source = "default"
        self.waiting = False

    def settings(self):
        return {"gain": self.gain, "floor": self.floor,
                "max": self.max_level, "stereo": self.stereo,
                "enabled": self.enabled, "source": self.source,
                "impact": self.impact, "impGain": self.impact_gain,
                "impMax": self.impact_max}

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
            self.level_l = self.level_r = self.rel = 0.0
            self.impact_l = self.impact_r = 0.0
            self.waiting = False
            log.info("audio mode OFF")

    def _stop_proc(self):
        if self._proc is not None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.terminate()
            self._proc = None

    def _resolve_target(self):
        """Translate self.source into parec args; None if not available yet."""
        src = self.source
        if src.startswith("sink:"):
            return ["-d", src[5:] + ".monitor"]
        if src.startswith("appname:"):
            name = src[8:].lower()
            try:
                inputs = json.loads(subprocess.run(
                    ["pactl", "-f", "json", "list", "sink-inputs"],
                    capture_output=True, text=True).stdout)
            except Exception:
                return None
            for si in inputs:
                props = si.get("properties", {})
                app = (props.get("application.name")
                       or props.get("application.process.binary") or "")
                if app.lower() == name:
                    return [f"--monitor-stream={si['index']}"]
            return None
        if src.startswith("app:"):  # legacy raw index
            return [f"--monitor-stream={src[4:]}"]
        sink = subprocess.run(
            ["pactl", "get-default-sink"], capture_output=True, text=True
        ).stdout.strip()
        return ["-d", sink + ".monitor"]

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
            self._proc = await asyncio.create_subprocess_exec(
                "parec", "--format=s16le", f"--rate={AUDIO_RATE}", "--channels=2",
                "--latency-msec=20", *target,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            log.info("audio capture started (%s)", " ".join(target))
            await self._pump()
            self._stop_proc()
            await asyncio.sleep(1)

    async def _pump(self):
        sm_l = sm_r = 0.0
        peak = 1e-3
        hf_base_l = hf_base_r = 0.0
        hf_peak = 1e-3
        imp_l = imp_r = 0.0
        bin_hz = AUDIO_RATE / AUDIO_CHUNK
        bass_hi = max(2, int(self.bass_hz / bin_hz))
        hf_lo, hf_hi = int(1000 / bin_hz), int(10000 / bin_hz)

        def bands(ch):
            spectrum = np.abs(np.fft.rfft(ch))
            norm = np.sqrt(AUDIO_CHUNK)
            bass = float(np.sqrt(np.mean(spectrum[1:bass_hi] ** 2)) / norm)
            hf = float(np.sqrt(np.mean(spectrum[hf_lo:hf_hi] ** 2)) / norm)
            return bass, hf

        frame_bytes = AUDIO_CHUNK * 4
        try:
            while True:
                data = await self._proc.stdout.readexactly(frame_bytes)
                # drop backlog: any stall would otherwise leave stale audio
                # queued in the pipe forever, turning into permanent delay
                while len(self._proc.stdout._buffer) >= frame_bytes:
                    data = await self._proc.stdout.readexactly(frame_bytes)
                samples = (
                    np.frombuffer(data, dtype=np.int16)
                    .reshape(-1, 2).astype(np.float32) / 32768.0
                )
                e_l, hf_l = bands(samples[:, 0])
                e_r, hf_r = bands(samples[:, 1])
                if not self.stereo:
                    e_l = e_r = max(e_l, e_r)
                    hf_l = hf_r = max(hf_l, hf_r)

                # impacts: HF onsets (sword hits, gunshots) vs a slow baseline
                # so sustained noise (music, wind) never triggers
                imp_l *= 0.55  # fast decay: sharp tap, not a drone
                imp_r *= 0.55
                if self.impact:
                    hf_base_l = hf_base_l * 0.985 + hf_l * 0.015
                    hf_base_r = hf_base_r * 0.985 + hf_r * 0.015
                    hf_peak = max(hf_peak * 0.99975, hf_l, hf_r)
                    ithresh = min(0.65, max(0.05, 0.65 - 0.055 * self.impact_gain))
                    scale = self.impact_max / 15.0
                    on_l = (hf_l - hf_base_l * 1.5) / hf_peak
                    on_r = (hf_r - hf_base_r * 1.5) / hf_peak
                    if hf_l > self.floor * 0.5 and on_l > ithresh:
                        imp_l = max(imp_l, min(1.0, 0.3 + (on_l - ithresh) / (1 - ithresh)) * scale)
                    if hf_r > self.floor * 0.5 and on_r > ithresh:
                        imp_r = max(imp_r, min(1.0, 0.3 + (on_r - ithresh) / (1 - ithresh)) * scale)
                self.impact_l = imp_l if imp_l > 0.03 else 0.0
                self.impact_r = imp_r if imp_r > 0.03 else 0.0

                # bass rumble: fast attack, slower release envelopes
                sm_l = max(e_l, sm_l * 0.90)
                sm_r = max(e_r, sm_r * 0.90)
                loud = max(sm_l, sm_r)
                self.thresh = min(0.9, max(0.06, 0.9 - 0.09 * self.gain))
                if loud < self.floor:
                    self.level_l = self.level_r = self.rel = 0.0
                    continue
                # slow-decaying loudness reference: react to *relative* loudness
                # so only the louder beats thump instead of a constant buzz
                peak = max(peak * 0.99975, loud)
                self.rel = loud / peak

                def out(sm):
                    rel = sm / peak
                    if rel <= self.thresh:
                        return 0.0
                    return ((rel - self.thresh) / (1 - self.thresh)) ** 1.5 * (
                        self.max_level / 15.0
                    )

                self.level_l, self.level_r = out(sm_l), out(sm_r)
        except asyncio.CancelledError:
            self.level_l = self.level_r = self.rel = 0.0
            self.impact_l = self.impact_r = 0.0
            raise
        except asyncio.IncompleteReadError:
            log.info("audio stream ended, will rebind")
            self.level_l = self.level_r = self.rel = 0.0
            self.impact_l = self.impact_r = 0.0
        except Exception as e:
            log.warning("audio capture error (will retry): %s", e)
            self.level_l = self.level_r = self.rel = 0.0
            self.impact_l = self.impact_r = 0.0


SDK2_CACHE = BASE_DIR / "sdk2_cache"
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
        self.osc_last = 0.0

    @property
    def osc_active(self):
        return (time.monotonic() - self.osc_last) < 5.0
        self.sdk2_events = {}  # eventName -> project dict (or None if unparseable)

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
                "Presets": sorted(self.presets.keys()),
                "ActivePreset": self.active_preset,
                "GameClients": sorted(set(self.game_clients.values()))
                + (["VRChat (OSC)"] if self.osc_active else []),
                "Effects": sorted(k for k in self.effects if not k.startswith("__")),
                "Battery": self.vest.battery,
            }
        )

    async def handle(self, payload):
        for reg in payload.get("Register") or []:
            key = reg.get("Key")
            project = reg.get("Project")
            if key and project:
                self.registered[key] = project
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
                self.active_preset = ""
                enabled = bool(am.get("enabled", True))
            else:
                enabled = bool(am)
            await self.audio.set_enabled(enabled)

        if "SavePreset" in payload:
            name = str(payload["SavePreset"]).strip()
            if name:
                snap = self.audio.settings()
                snap.pop("enabled", None)
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

        if "DeleteEffect" in payload:
            if self.effects.pop(str(payload["DeleteEffect"]), None) is not None:
                self._save_effects()

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
        new_src = str(d.get("source", a.source))
        if new_src != a.source:
            was_enabled = a.enabled
            await a.set_enabled(False)
            a.source = new_src
            if was_enabled:
                await a.set_enabled(True)

    def _save_presets(self):
        with contextlib.suppress(OSError):
            PRESETS_FILE.write_text(json.dumps(self.presets, indent=1))

    def ingest_sdk2_defs(self, data, source):
        if data is None:
            return
        SDK2_CACHE.mkdir(exist_ok=True)
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
                    and (a.level_l or a.level_r or a.impact_l or a.impact_r)):
                l, r = a.level_l, a.level_r
                il, ir = a.impact_l, a.impact_r
                wl = (1.0, 0.67, 0.33, 0.0)  # grid col 0 = wearer's left
                for i in range(20):
                    w = wl[i % 4]
                    lvl = l * w + r * (1 - w)
                    front[i] = max(front[i], lvl)
                    back[i] = max(back[i], lvl)
                    if i < 8:  # impacts hit the upper two rows only
                        ilvl = il * w + ir * (1 - w)
                        front[i] = max(front[i], ilvl)
                        back[i] = max(back[i], ilvl)

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

    async def meter_loop(self):
        while True:
            if self.meter_subs:
                a = self.audio
                msg = json.dumps({"AudioMeter": {
                    "on": a.enabled, "rel": round(a.rel, 3), "thresh": round(a.thresh, 3),
                    "outL": round(a.level_l, 3), "outR": round(a.level_r, 3),
                    "impL": round(a.impact_l, 3), "impR": round(a.impact_r, 3),
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
            await state.handle(payload)
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

    cert = BASE_DIR / "sdk2_cert.pem"
    key = BASE_DIR / "sdk2_key.pem"
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
    vest = VestLink()
    state = PlayerState(vest)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, stop.set)

    if state.audio.start_enabled:
        await state.audio.set_enabled(True)

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
    ]

    async with websockets.serve(
        lambda ws: ws_handler(ws, state), "127.0.0.1", 15881,
        process_request=process_request,
    ), websockets.serve(
        lambda ws: sdk2_handler(ws, state), "127.0.0.1", 15882,
        ssl=sdk2_ssl_context(),
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
