"""Dumb audio-to-haptics for bHaptics TactSuitPro over BLE.

Captures the default sink monitor via parec, maps bass/mid energy to motor
intensity, streams 20-byte frames to the vest at ~25 Hz. Ctrl-C to stop.
"""
import argparse
import asyncio
import contextlib
import signal
import subprocess
import sys

import numpy as np
from bleak import BleakClient

import sys
if len(sys.argv) < 2 or sys.argv[1].count(":") != 5:
    sys.exit("usage: audio_haptics.py <BLE-MAC-of-vest> [options]")
ADDR = sys.argv[1]
MOTOR_STABLE = "6e40000a-b5a3-f393-e0a9-e50e24dcca9e"
RATE = 48000
CHUNK = 1920  # 40 ms @ 48 kHz

def pack(values):
    return bytes((min(values[i * 2], 15) << 4) | min(values[i * 2 + 1], 15) for i in range(20))

def frame_uniform(level):
    return pack([level] * 40)

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gain", type=float, default=4.0, help="sensitivity multiplier")
    ap.add_argument("--floor", type=float, default=0.015, help="noise gate (0-1 RMS)")
    ap.add_argument("--max", type=int, default=11, help="max motor intensity 1-15")
    ap.add_argument("--bass-hz", type=float, default=250.0, help="only react below this frequency")
    ap.add_argument("--addr", default=ADDR)
    args = ap.parse_args()

    sink = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True).stdout.strip()
    monitor = sink + ".monitor"
    print(f"capturing from {monitor}")

    rec = await asyncio.create_subprocess_exec(
        "parec", "--format=s16le", f"--rate={RATE}", "--channels=1", "-d", monitor,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )

    fft_keep = int(args.bass_hz * CHUNK / RATE)
    smoothed = 0.0
    last_level = -1

    async with BleakClient(args.addr, timeout=30.0) as client:
        print("vest connected — play some music. Ctrl-C to stop.")

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for s in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(s, stop.set)

        async def pump():
            nonlocal smoothed, last_level
            while not stop.is_set():
                data = await rec.stdout.readexactly(CHUNK * 2)
                samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                spectrum = np.abs(np.fft.rfft(samples))[1:fft_keep]
                energy = np.sqrt(np.mean(spectrum**2)) / np.sqrt(CHUNK) if len(spectrum) else 0.0
                # fast attack, slower release
                smoothed = max(energy, smoothed * 0.82)
                level = 0
                if smoothed > args.floor:
                    level = min(args.max, int((smoothed - args.floor) * args.gain * 60))
                if level != last_level:
                    await client.write_gatt_char(MOTOR_STABLE, frame_uniform(level), response=False)
                    last_level = level
                bar = "#" * level
                print(f"\r level {level:2d} |{bar:<15}|", end="", flush=True)

        task = asyncio.create_task(pump())
        await stop.wait()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await client.write_gatt_char(MOTOR_STABLE, frame_uniform(0), response=False)
        print("\nmotors off, bye")
    rec.terminate()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
