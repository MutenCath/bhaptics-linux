"""vestctl's rpc() against a fake daemon: timeouts, missing keys, happy path."""
import asyncio
import json
import unittest

import tests.base  # noqa: F401  (sys.path setup for the repo root)
import vestctl
import websockets


class RpcTest(unittest.TestCase):
    def setUp(self):
        self._uri = vestctl.URI
        self.addCleanup(setattr, vestctl, "URI", self._uri)

    def _run(self, handler, call):
        async def flow():
            server = await websockets.serve(handler, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            vestctl.URI = f"ws://127.0.0.1:{port}/v2/feedbacks"
            try:
                await call()
            finally:
                server.close()
        asyncio.run(flow())

    def test_silent_daemon_times_out_instead_of_hanging(self):
        async def silent(ws):
            await asyncio.sleep(30)

        async def call():
            with self.assertRaises(asyncio.TimeoutError):
                await vestctl.rpc([])
        self._run(silent, call)

    def test_missing_wanted_key_raises_connection_error(self):
        async def no_wanted_key(ws):
            await ws.send(json.dumps({"Status": True}))
            async for _raw in ws:
                await ws.send(json.dumps({"Other": True}))

        async def call():
            with self.assertRaises(ConnectionError):
                await vestctl.rpc([{"ListAudioSources": True}], want="AudioSources")
        self._run(no_wanted_key, call)

    def test_happy_path_returns_reply_and_status(self):
        async def echo(ws):
            await ws.send(json.dumps({"Status": True, "ConnectedDeviceCount": 1}))
            async for raw in ws:
                json.loads(raw)
                await ws.send(json.dumps({"AudioSources": []}))

        async def call():
            reply, status = await vestctl.rpc([{"ListAudioSources": True}],
                                              want="AudioSources")
            self.assertEqual(reply["AudioSources"], [])
            self.assertEqual(status["ConnectedDeviceCount"], 1)
        self._run(echo, call)


if __name__ == "__main__":
    unittest.main()
