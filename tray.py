#!/usr/bin/env python3
"""Optional system-tray quick control for the bHaptics vest.

A thin, headless controller (`TrayController`) builds the tray menu from the
daemon's status; the GUI half uses `pystray` + `Pillow`, which are optional
dependencies. Run it with `python tray.py`, or point a desktop autostart entry
at it. Without those packages it prints how to install them and exits.
"""
import asyncio
import contextlib
import json
import sys
import threading
import time
import webbrowser

URI = "ws://127.0.0.1:15881/v2/feedbacks?app_id=ui&app_name=bhaptics-tray"
UI_URL = "http://127.0.0.1:15881/ui"


async def _rpc(messages):
    """One short-lived request/reply round-trip with the daemon."""
    import websockets

    async with websockets.connect(URI, open_timeout=3) as ws:
        status = json.loads(await ws.recv())
        for msg in messages:
            await ws.send(json.dumps(msg))
            await ws.recv()
        return status


class TrayController:
    """Status + menu/action logic, independent of any GUI toolkit."""

    def __init__(self):
        self.status = {}
        self.online = False
        self.stop = False

    # ---- status -----------------------------------------------------------
    def refresh(self, status):
        self.online = True
        self.status = status
        return self.title

    def set_offline(self):
        self.online = False
        self.status = {}
        return self.title

    @property
    def title(self):
        if not self.online:
            return "bHaptics — daemon unreachable"
        s = self.status
        if not s.get("ConnectedDeviceCount"):
            return ("bHaptics — vest idle" if s.get("VestIdle")
                    else "bHaptics — searching for vest")
        bits = ["bHaptics", "vest connected"]
        if s.get("Battery") is not None:
            bits.append(f"{s['Battery']}%")
        if s.get("AutoProfileApp"):
            bits.append(s["AutoProfileApp"])
        return " — ".join(bits)

    # ---- menu -------------------------------------------------------------
    def menu_rows(self):
        """Flat menu as `[(label, action, enabled)]`; action None = info row."""
        if not self.online:
            return [("Daemon unreachable", None, False),
                    ("Open UI", "open_ui", True),
                    ("Quit", "quit", True)]
        s = self.status
        audio = "on" if s.get("AudioMode") else "off"
        if s.get("AudioSuppressed"):
            audio += " (paused — game)"
        rows = [("Audio haptics: " + audio, "toggle_audio", True),
                ("Source: " + str(s.get("AudioSource", "?")), None, False)]
        if s.get("AutoProfileApp"):
            rows.append(("Active: " + s["AutoProfileApp"], None, False))
        if s.get("VestIdle"):
            rows.append(("Wake the vest", "wake", True))
        rows += [("Arm for games (auto)", "arm", True),
                 ("Stop all effects", "stop", True),
                 ("Open UI", "open_ui", True),
                 ("Quit", "quit", True)]
        return rows

    # ---- actions ----------------------------------------------------------
    def _send(self, message):
        with contextlib.suppress(Exception):
            asyncio.run(_rpc([message]))

    def run_action(self, action):
        if action == "open_ui":
            webbrowser.open(UI_URL)
        elif action == "toggle_audio":
            on = not bool(self.status.get("AudioMode"))
            self._send({"AudioMode": True} if on else {"AudioMode": False})
        elif action == "arm":
            self._send({"AudioMode": {"enabled": True, "auto": True,
                                      "source": "auto"}})
        elif action == "wake":
            self._send({"Wake": True})
        elif action == "stop":
            self._send({"Submit": [{"Type": "turnOffAll"}]})
        elif action == "quit":
            self.stop = True


def _poll(controller, on_change):
    """Background poller; the daemon has no push that a tray can rely on."""
    signature = None
    while not controller.stop:
        try:
            status = asyncio.run(_rpc([]))
            controller.refresh(status)
        except Exception:
            controller.set_offline()
        # the title alone misses menu-only changes (audio on/off, source)
        sig = (controller.title, controller.menu_rows())
        if sig != signature:
            signature = sig
            with contextlib.suppress(Exception):
                on_change()
        time.sleep(2)


def _icon_image():
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([10, 12, 54, 52], radius=11, fill=(77, 163, 255, 255))
    d.rectangle([4, 16, 12, 48], fill=(77, 163, 255, 255))
    d.rectangle([52, 16, 60, 48], fill=(77, 163, 255, 255))
    return img


def main():
    try:
        import pystray
        from pystray import Menu, MenuItem
    except ImportError:
        print("tray: pystray missing — rerun ./install.sh (git checkout) or\n"
              "  sudo pacman -S python-pystray python-gobject", file=sys.stderr)
        return 2

    controller = TrayController()

    def do(action):
        if action == "quit":
            controller.stop = True
            with contextlib.suppress(Exception):
                icon.stop()
            return
        controller.run_action(action)

    def handler(action):
        return lambda icon, item: do(action)

    def build_items():
        items = []
        for label, action, enabled in controller.menu_rows():
            if action is None:
                items.append(MenuItem(label, None, enabled=False))
            else:
                items.append(MenuItem(label, handler(action), enabled=enabled))
        return items

    icon = pystray.Icon("bhaptics", _icon_image(), "bHaptics",
                        Menu(build_items))
    def on_change():
        icon.title = controller.title  # tooltip shows vest/battery/game
        icon.update_menu()

    threading.Thread(target=_poll,
                     args=(controller, on_change), daemon=True).start()
    icon.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
