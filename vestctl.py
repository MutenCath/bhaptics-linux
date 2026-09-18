"""Command-line control for the bhaptics-linux daemon."""
import argparse
import asyncio
import json
import sys

import websockets

URI = "ws://127.0.0.1:15881/v2/feedbacks?app_id=ui&app_name=vestctl"


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


def main():
    ap = argparse.ArgumentParser(prog="vestctl", description="Control the bHaptics vest daemon")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="vest/audio/game status")
    p = sub.add_parser("pulse", help="buzz the vest")
    p.add_argument("where", nargs="?", default="all", choices=["all", "front", "back"])
    p.add_argument("-i", "--intensity", type=int, default=60, help="0-100")
    p.add_argument("-d", "--duration", type=int, default=300, help="ms")
    sub.add_parser("stop", help="stop all effects")
    p = sub.add_parser("audio", help="audio mode on/off")
    p.add_argument("state", choices=["on", "off"])
    p = sub.add_parser("preset", help="list or apply presets")
    p.add_argument("name", nargs="?")
    p = sub.add_parser("effect", help="list or play saved effects")
    p.add_argument("name", nargs="?")
    sub.add_parser("sources", help="list audio sources")
    args = ap.parse_args()

    async def run():
        if args.cmd == "status":
            _, st = await rpc([])
            vest = "connected" if st["ConnectedDeviceCount"] else "NOT FOUND"
            bat = f" battery {st['Battery']}%" if st.get("Battery") is not None else ""
            audio = "on" if st["AudioMode"] else "off"
            if st.get("AudioSuppressed"):
                audio += " (paused: game active)"
            print(f"vest: {vest}{bat}")
            print(f"audio: {audio}  source: {st['AudioSource']}  preset: {st['ActivePreset'] or '-'}")
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
