import asyncio
import sys
from bleak import BleakClient

if len(sys.argv) < 2:
    sys.exit("usage: test_motors.py <BLE-MAC-of-vest>  (find it with scan.py)")
ADDR = sys.argv[1]
MOTOR_STABLE = "6e40000a-b5a3-f393-e0a9-e50e24dcca9e"

def frame(values):
    values = (list(values) + [0] * 40)[:40]
    return bytes((min(values[i * 2], 15) << 4) | min(values[i * 2 + 1], 15) for i in range(20))

async def main():
    async with BleakClient(ADDR, timeout=30.0) as client:
        print("connected, sending gentle all-motor pulse (3/15)...")
        await client.write_gatt_char(MOTOR_STABLE, frame([3] * 40), response=False)
        await asyncio.sleep(0.7)
        await client.write_gatt_char(MOTOR_STABLE, frame([0] * 40), response=False)
        await asyncio.sleep(0.5)

        print("stronger pulse on first motor block (8/15)...")
        await client.write_gatt_char(MOTOR_STABLE, frame([8] * 6), response=False)
        await asyncio.sleep(0.5)
        await client.write_gatt_char(MOTOR_STABLE, frame([0] * 40), response=False)
        print("done, motors off")

asyncio.run(main())
