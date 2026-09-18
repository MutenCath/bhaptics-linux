"""Pretends to be a bHaptics-enabled game talking to the Player websocket."""
import asyncio
import json

import websockets

URI = "ws://127.0.0.1:15881/v2/feedbacks?app_id=test&app_name=TestGame"

HEARTBEAT_PROJECT = {
    "Tracks": [
        {
            "Effects": [
                {
                    "StartTime": 0,
                    "OffsetTime": 0,
                    "Modes": {
                        "VestFront": {
                            "DotMode": {
                                "Feedback": [
                                    {"StartTime": 0, "EndTime": 250,
                                     "PointList": [{"Index": 9, "Intensity": 0.8},
                                                   {"Index": 10, "Intensity": 0.8}]},
                                    {"StartTime": 350, "EndTime": 550,
                                     "PointList": [{"Index": 9, "Intensity": 0.5},
                                                   {"Index": 10, "Intensity": 0.5}]},
                                ]
                            }
                        }
                    },
                }
            ]
        }
    ]
}

async def main():
    async with websockets.connect(URI) as ws:
        print("status:", await ws.recv())

        print("frame submit: back shoulder blades, 300ms")
        await ws.send(json.dumps({"Submit": [{"Type": "frame", "Key": "hit1", "Frame": {
            "Position": "VestBack", "DurationMillis": 300,
            "DotPoints": [{"Index": 1, "Intensity": 70}, {"Index": 2, "Intensity": 70}]}}]}))
        print("status:", await ws.recv())
        await asyncio.sleep(0.8)

        print("registering heartbeat pattern")
        await ws.send(json.dumps({"Register": [{"Key": "heartbeat", "Project": HEARTBEAT_PROJECT}]}))
        print("status:", await ws.recv())

        for beat in range(3):
            await ws.send(json.dumps({"Submit": [{"Type": "key", "Key": "heartbeat",
                                                  "Parameters": {"intensityRatio": 1.0}}]}))
            await ws.recv()
            await asyncio.sleep(0.9)
        await asyncio.sleep(0.8)
        print("done")

asyncio.run(main())
