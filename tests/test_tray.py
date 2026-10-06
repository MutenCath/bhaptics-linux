"""Tray controller logic (no GUI): titles, menu rows, action payloads."""
import sys
import types
import unittest

import tray


class TrayControllerTest(unittest.TestCase):
    def test_title_offline(self):
        self.assertIn("unreachable", tray.TrayController().title)

    def test_title_idle_vs_searching(self):
        c = tray.TrayController()
        c.refresh({"ConnectedDeviceCount": 0, "VestIdle": True})
        self.assertIn("idle", c.title)
        c.refresh({"ConnectedDeviceCount": 0, "VestIdle": False})
        self.assertIn("searching", c.title)

    def test_title_active_game(self):
        c = tray.TrayController()
        c.refresh({"ConnectedDeviceCount": 1, "Battery": 87,
                   "AutoProfileApp": "Doom"})
        self.assertIn("Doom", c.title)
        self.assertIn("87%", c.title)

    def test_menu_offline_offers_quit_and_ui(self):
        actions = [a for _, a, _ in tray.TrayController().menu_rows() if a]
        self.assertIn("quit", actions)
        self.assertIn("open_ui", actions)

    def _capture(self):
        sent = []

        async def fake(msgs):
            sent.append(msgs)

        original = tray._rpc
        tray._rpc = fake
        self.addCleanup(setattr, tray, "_rpc", original)
        return sent

    def test_arm_sends_games_only(self):
        c = tray.TrayController()
        c.refresh({"ConnectedDeviceCount": 1, "AudioMode": False})
        sent = self._capture()
        c.run_action("arm")
        self.assertEqual(sent[0][0]["AudioMode"]["source"], "auto")
        self.assertTrue(sent[0][0]["AudioMode"]["enabled"])

    def test_toggle_audio_turns_off_when_on(self):
        c = tray.TrayController()
        c.refresh({"ConnectedDeviceCount": 1, "AudioMode": True})
        sent = self._capture()
        c.run_action("toggle_audio")
        self.assertIs(sent[0][0]["AudioMode"], False)

    def test_stop_sends_turn_off_all(self):
        c = tray.TrayController()
        c.refresh({"ConnectedDeviceCount": 1})
        sent = self._capture()
        c.run_action("stop")
        self.assertEqual(sent[0][0]["Submit"][0]["Type"], "turnOffAll")


class FakeMenuItem:
    def __init__(self, label, action, enabled=True, **kw):
        self.label = label
        self.action = action
        self.enabled = enabled


class FakeMenu:
    def __init__(self, *items):
        self._items = items

    @property
    def items(self):
        if len(self._items) == 1 and callable(self._items[0]):
            return list(self._items[0]())
        return list(self._items)


class FakeIcon:
    last = None

    def __init__(self, name, icon, title, menu):
        self.menu = menu
        self.stopped = False
        FakeIcon.last = self

    def update_menu(self):
        pass

    def run(self):
        pass

    def stop(self):
        self.stopped = True


class OnlineController(tray.TrayController):
    def __init__(self):
        super().__init__()
        self.refresh({"ConnectedDeviceCount": 1, "AudioMode": False, "Battery": 90})


class TrayGuiWiringTest(unittest.TestCase):
    """Exercise main()'s pystray glue with a fake toolkit (no display)."""

    def setUp(self):
        pystray = types.ModuleType("pystray")
        pystray.Menu = FakeMenu
        pystray.MenuItem = FakeMenuItem
        pystray.Icon = FakeIcon
        pil = types.ModuleType("PIL")
        image = types.ModuleType("PIL.Image")
        image.new = lambda *a, **k: object()
        draw = types.ModuleType("PIL.ImageDraw")
        draw.Draw = lambda img: types.SimpleNamespace(
            rounded_rectangle=lambda *a, **k: None,
            rectangle=lambda *a, **k: None)
        pil.Image, pil.ImageDraw = image, draw
        self._mods = {n: sys.modules.get(n) for n in ("pystray", "PIL",
                                                      "PIL.Image", "PIL.ImageDraw")}
        sys.modules.update({"pystray": pystray, "PIL": pil,
                            "PIL.Image": image, "PIL.ImageDraw": draw})
        self.addCleanup(self._restore)

        self.sent = []

        async def fake(msgs):
            self.sent.append(msgs)

        self._rpc = tray._rpc
        tray._rpc = fake
        self.addCleanup(setattr, tray, "_rpc", self._rpc)
        self._poll = tray._poll
        tray._poll = lambda controller, on_change: None
        self.addCleanup(setattr, tray, "_poll", self._poll)
        self._controller = tray.TrayController
        tray.TrayController = OnlineController
        self.addCleanup(setattr, tray, "TrayController", self._controller)

    def _restore(self):
        for name, mod in self._mods.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    def _items(self):
        tray.main()  # with the fake toolkit, returns immediately
        return {i.label: i for i in FakeIcon.last.menu.items}

    def test_menu_action_is_invokable_with_pystray_arity(self):
        items = self._items()
        arm = next(i for label, i in items.items() if label.startswith("Arm"))
        # pystray calls actions with (icon, item); make sure that works
        arm.action(FakeIcon.last, arm)
        self.assertEqual(self.sent[0][0]["AudioMode"]["source"], "auto")

    def test_quit_stops_icon(self):
        items = self._items()
        items["Quit"].action(FakeIcon.last, items["Quit"])
        self.assertTrue(FakeIcon.last.stopped)


if __name__ == "__main__":
    unittest.main()
